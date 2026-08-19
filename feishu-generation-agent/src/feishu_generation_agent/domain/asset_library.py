from datetime import datetime, timezone
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator


ASSET_IMAGE_MIME_TYPES = frozenset(
    {"image/png", "image/jpeg", "image/webp", "image/gif"}
)


class AssetKind(StrEnum):
    CHARACTER = "character"
    STYLE = "style"
    SCENE = "scene"
    PROP = "prop"


def normalize_alias(value: str) -> str:
    stripped = value.strip()
    return stripped.lower() if stripped.isascii() else stripped


class CharacterAsset(BaseModel):
    asset_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    variant: str = Field(min_length=1)
    kind: AssetKind = AssetKind.CHARACTER
    description: str = ""
    aliases: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    model_prefs: list[str] = Field(default_factory=list)
    prompt_fragment: str = ""
    storage_path: str = Field(min_length=1)
    storage_url: str = Field(min_length=1)
    mime_type: str
    byte_size: int = Field(ge=0)
    volcengine_asset_id: str | None = None
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    @field_validator("name", "variant")
    @classmethod
    def require_non_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("字段不能为空白")
        return stripped

    @field_validator("mime_type")
    @classmethod
    def require_image_mime(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in ASSET_IMAGE_MIME_TYPES:
            raise ValueError(f"素材只支持图片类型，收到 {value!r}")
        return normalized

    @field_validator("aliases", "tags", "model_prefs")
    @classmethod
    def clean_string_list(cls, value: list[str]) -> list[str]:
        seen: list[str] = []
        for item in value:
            stripped = item.strip()
            if stripped and stripped not in seen:
                seen.append(stripped)
        return seen

    def model_post_init(self, _context: object) -> None:
        deduped = [
            alias
            for alias in self.aliases
            if normalize_alias(alias) != normalize_alias(self.name)
        ]
        object.__setattr__(self, "aliases", deduped)

    def match_keys(self) -> tuple[str, ...]:
        keys = [normalize_alias(self.name)]
        for alias in self.aliases:
            key = normalize_alias(alias)
            if key not in keys:
                keys.append(key)
        return tuple(keys)


class AssetQuery(BaseModel):
    name: str | None = None
    kind: AssetKind | None = None
    tags: list[str] = Field(default_factory=list)
    limit: int = Field(default=100, ge=1, le=500)
    offset: int = Field(default=0, ge=0)
