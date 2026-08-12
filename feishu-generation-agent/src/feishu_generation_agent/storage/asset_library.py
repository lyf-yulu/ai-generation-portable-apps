from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import uuid4

import aiosqlite

from feishu_generation_agent.domain.asset_library import (
    ASSET_IMAGE_MIME_TYPES,
    AssetKind,
    CharacterAsset,
)


_MIME_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS asset_library (
    asset_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    variant TEXT NOT NULL,
    kind TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    aliases TEXT NOT NULL DEFAULT '[]',
    tags TEXT NOT NULL DEFAULT '[]',
    model_prefs TEXT NOT NULL DEFAULT '[]',
    prompt_fragment TEXT NOT NULL DEFAULT '',
    storage_path TEXT NOT NULL,
    storage_url TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    byte_size INTEGER NOT NULL,
    volcengine_asset_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (name, variant)
);
CREATE INDEX IF NOT EXISTS idx_asset_library_name ON asset_library (name);
CREATE INDEX IF NOT EXISTS idx_asset_library_kind ON asset_library (kind);
"""


class DuplicateAssetError(ValueError):
    """同名 + 同 variant 的素材已存在。"""


class AssetLibraryStore:
    def __init__(
        self,
        connection: aiosqlite.Connection,
        assets_dir: Path,
        base_url: str,
    ) -> None:
        self._connection = connection
        self._assets_dir = assets_dir
        self._base_url = base_url.rstrip("/")

    @classmethod
    async def open(
        cls,
        *,
        db_path: Path,
        assets_dir: Path,
        base_url: str,
    ) -> "AssetLibraryStore":
        db_path.parent.mkdir(parents=True, exist_ok=True)
        assets_dir.mkdir(parents=True, exist_ok=True)
        connection = await aiosqlite.connect(db_path)
        connection.row_factory = aiosqlite.Row
        await connection.execute("PRAGMA journal_mode=WAL")
        await connection.execute("PRAGMA foreign_keys=ON")
        await connection.executescript(_SCHEMA)
        await connection.commit()
        return cls(connection, assets_dir, base_url)

    async def close(self) -> None:
        await self._connection.close()

    async def create(
        self,
        *,
        name: str,
        variant: str,
        content: bytes,
        mime_type: str,
        kind: AssetKind = AssetKind.CHARACTER,
        description: str = "",
        aliases: list[str] | None = None,
        tags: list[str] | None = None,
        model_prefs: list[str] | None = None,
        prompt_fragment: str = "",
    ) -> CharacterAsset:
        normalized_mime = mime_type.strip().lower()
        if normalized_mime not in ASSET_IMAGE_MIME_TYPES:
            raise ValueError(f"素材只支持图片类型，收到 {mime_type!r}")
        if not content:
            raise ValueError("素材文件内容为空")

        asset_id = uuid4().hex
        extension = _MIME_EXTENSIONS[normalized_mime]
        relative_path = f"{self._assets_dir.name}/{asset_id}{extension}"
        now = datetime.now(timezone.utc)
        asset = CharacterAsset(
            asset_id=asset_id,
            name=name,
            variant=variant,
            kind=kind,
            description=description,
            aliases=aliases or [],
            tags=tags or [],
            model_prefs=model_prefs or [],
            prompt_fragment=prompt_fragment,
            storage_path=relative_path,
            storage_url=f"{self._base_url}/{relative_path}",
            mime_type=normalized_mime,
            byte_size=len(content),
            created_at=now,
            updated_at=now,
        )

        try:
            await self._connection.execute(
                """
                INSERT INTO asset_library (
                    asset_id, name, variant, kind, description, aliases, tags,
                    model_prefs, prompt_fragment, storage_path, storage_url,
                    mime_type, byte_size, volcengine_asset_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    asset.asset_id,
                    asset.name,
                    asset.variant,
                    asset.kind.value,
                    asset.description,
                    json.dumps(asset.aliases, ensure_ascii=False),
                    json.dumps(asset.tags, ensure_ascii=False),
                    json.dumps(asset.model_prefs, ensure_ascii=False),
                    asset.prompt_fragment,
                    asset.storage_path,
                    asset.storage_url,
                    asset.mime_type,
                    asset.byte_size,
                    None,
                    asset.created_at.isoformat(),
                    asset.updated_at.isoformat(),
                ),
            )
        except aiosqlite.IntegrityError as error:
            raise DuplicateAssetError(
                f"素材已存在：{name} / {variant}"
            ) from error

        target = self._assets_dir / f"{asset_id}{extension}"
        target.write_bytes(content)
        await self._connection.commit()
        return asset

    async def get(self, asset_id: str) -> CharacterAsset | None:
        cursor = await self._connection.execute(
            "SELECT * FROM asset_library WHERE asset_id = ?", (asset_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return self._row_to_asset(row) if row is not None else None

    async def list(
        self,
        *,
        kind: AssetKind | None = None,
        name: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[CharacterAsset]:
        clauses: list[str] = []
        parameters: list[object] = []
        if kind is not None:
            clauses.append("kind = ?")
            parameters.append(kind.value)
        if name:
            clauses.append("name LIKE ?")
            parameters.append(f"%{name}%")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.extend([limit, offset])
        cursor = await self._connection.execute(
            f"SELECT * FROM asset_library{where} "
            "ORDER BY name, variant LIMIT ? OFFSET ?",
            tuple(parameters),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [self._row_to_asset(row) for row in rows]

    def _row_to_asset(self, row: aiosqlite.Row) -> CharacterAsset:
        return CharacterAsset(
            asset_id=row["asset_id"],
            name=row["name"],
            variant=row["variant"],
            kind=AssetKind(row["kind"]),
            description=row["description"],
            aliases=json.loads(row["aliases"]),
            tags=json.loads(row["tags"]),
            model_prefs=json.loads(row["model_prefs"]),
            prompt_fragment=row["prompt_fragment"],
            storage_path=row["storage_path"],
            storage_url=row["storage_url"],
            mime_type=row["mime_type"],
            byte_size=row["byte_size"],
            volcengine_asset_id=row["volcengine_asset_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )
