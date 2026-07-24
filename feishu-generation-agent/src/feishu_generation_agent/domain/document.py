import hashlib
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class SourceType(StrEnum):
    DOCX = "docx"
    WIKI = "wiki"


class PlanningPromptSnapshot(BaseModel):
    owner_user_id: str = Field(min_length=1, max_length=255)
    source: Literal["prime", "personal"]
    version: int = Field(ge=0)
    prompt_text: str = Field(min_length=1)
    prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_prompt_hash(self) -> "PlanningPromptSnapshot":
        expected = hashlib.sha256(self.prompt_text.encode("utf-8")).hexdigest()
        if self.prompt_sha256 != expected:
            raise ValueError("prompt_sha256 does not match prompt_text")
        return self


def build_planning_prompt_snapshot(
    *,
    owner_user_id: str,
    source: Literal["prime", "personal"],
    version: int,
    prompt_text: str,
) -> PlanningPromptSnapshot:
    return PlanningPromptSnapshot(
        owner_user_id=owner_user_id,
        source=source,
        version=version,
        prompt_text=prompt_text,
        prompt_sha256=hashlib.sha256(prompt_text.encode("utf-8")).hexdigest(),
    )


class RequirementRequest(BaseModel):
    source_url: str
    requester_open_id: str | None = None
    trigger_type: str = "local_link"
    reply_context: dict[str, str] = Field(default_factory=dict)
    planning_prompt: PlanningPromptSnapshot | None = None


class DocumentBlock(BaseModel):
    block_id: str
    parent_id: str | None
    block_type: str
    order: int
    path: list[str]
    text: str = ""
    table_row: int | None = None
    table_column: int | None = None
    image_asset_id: str | None = None


class MediaAsset(BaseModel):
    asset_id: str
    source_block_id: str
    origin: str
    file_token: str | None = None
    local_path: Path
    mime_type: str
    size: int
    sha256: str
    width: int | None = None
    height: int | None = None
    download_error: str | None = None


class VisionDescription(BaseModel):
    asset_id: str
    subjects: list[str]
    scene: str
    style: str
    composition: str
    characters: list[str]
    actions: list[str]
    visible_text: list[str]
    colors: list[str]
    probable_role: str
    uncertainties: list[str]


class NormalizedDocument(BaseModel):
    document_id: str
    title: str
    revision: int
    source_type: SourceType
    source_token: str
    blocks: list[DocumentBlock]
    text_view: str
    media_assets: list[MediaAsset]
    ingest_issues: list[str] = Field(default_factory=list)
