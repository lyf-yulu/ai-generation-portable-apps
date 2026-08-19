# 素材库（阶段 1）实施计划

> **给执行者：** 逐任务执行，每个 Step 完成后勾选。每个任务末尾必须 commit。
> 本阶段**只做本地素材库的增删改查 + 文件静态服务**，不含文档匹配、planner 改动、graph 集成、火山镜像——那些在阶段 2/3。
> 阶段 1 完成后的可验收状态：能通过 REST 手动录入 / 编辑 / 查询 / 删除角色素材，素材图片能通过稳定 URL 被外部 HTTP 客户端读取。

**Goal:** 给 feishu-generation-agent 加一个本地角色素材库（SQLite 元数据 + 本地文件 + 可配置 URL base），供后续图片模式的参考图自动挂载使用。

**Architecture:** 主存储是本地 SQLite（`data/asset-library.sqlite3`）+ 本地文件目录（`data/asset-library/`）。火山 Ark Asset 只在阶段 3 作为按需缓存镜像，本阶段只预留 `volcengine_asset_id` 字段不实现同步。素材 URL 通过 `Settings.asset_base_url` 配置化拼装，写入时存完整 URL 快照，禁止运行时拼接（服务机 LAN IP 每周会变）。

**Tech Stack:** Python 3.12、pydantic v2、aiosqlite、FastAPI、pytest（`asyncio_mode=auto`）。

---

## 背景约束（执行前必读）

1. **不要硬编码 `192.168.30.5`**。服务机 LAN IP 每周变动。所有对外 URL 走 `Settings.asset_base_url`。
2. **URL 存快照不存拼接**：`asset_library.storage_url` 存写入当时算出的完整 URL。换 base 时跑 migration 批量重写该列（阶段 1 提供该脚本）。
3. **同一人物不同着装要分开入库**：靠 `name` + `variant` 组合唯一，`variant` 是着装/状态标签（例：`Sarah` + `晚宴礼服`）。
4. 现有代码风格：`from __future__ import annotations` 不使用；类型标注用 `str | None` 原生写法；SQLite 层用 `aiosqlite`，参考 `src/feishu_generation_agent/storage/portrait_assets.py` 的建表/连接模式。
5. 测试放 `tests/unit/`，命名 `test_asset_library*.py`。pytest 已配 `asyncio_mode=auto`，async 测试**不需要**加 `@pytest.mark.asyncio`。
6. 跑测试统一用：`cd feishu-generation-agent && uv run pytest <路径> -v`

---

## 文件结构

| 文件 | 职责 |
|---|---|
| `src/feishu_generation_agent/domain/asset_library.py` | 新建。`CharacterAsset` / `AssetKind` / `AssetQuery` 领域模型与校验 |
| `src/feishu_generation_agent/storage/asset_library.py` | 新建。`AssetLibraryStore`：SQLite CRUD + 文件落盘 |
| `src/feishu_generation_agent/config.py` | 修改。加 `asset_library_db_path`、`asset_library_dir`、`asset_base_url` |
| `src/feishu_generation_agent/web/schemas.py` | 修改。加素材库 REST 的请求/响应 schema |
| `src/feishu_generation_agent/web/app.py` | 修改。加 `/api/asset-library/*` 路由 + `/asset-library` 静态挂载 |
| `src/feishu_generation_agent/bootstrap.py` | 修改。构造 `AssetLibraryStore` 并注入 |
| `scripts/rewrite_asset_urls.py` | 新建。换 URL base 时批量重写 `storage_url` 列 |
| `tests/unit/test_asset_library_domain.py` | 新建。领域模型测试 |
| `tests/unit/test_asset_library_store.py` | 新建。存储层测试 |
| `tests/unit/test_asset_library_api.py` | 新建。REST 层测试 |

---

## Task 1: 领域模型 CharacterAsset

**Files:**
- Create: `src/feishu_generation_agent/domain/asset_library.py`
- Test: `tests/unit/test_asset_library_domain.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/unit/test_asset_library_domain.py`：

```python
import pytest
from pydantic import ValidationError

from feishu_generation_agent.domain.asset_library import (
    AssetKind,
    CharacterAsset,
    normalize_alias,
)


def test_asset_requires_name_and_variant():
    asset = CharacterAsset(
        asset_id="a1",
        name="Sarah",
        variant="晚宴礼服",
        kind=AssetKind.CHARACTER,
        description="女主，金色长发",
        aliases=["莎拉"],
        tags=["女主", "爽剧"],
        model_prefs=["seedream"],
        storage_path="asset-library/a1.png",
        storage_url="https://example.com/asset-library/a1.png",
        mime_type="image/png",
        byte_size=1024,
    )
    assert asset.name == "Sarah"
    assert asset.variant == "晚宴礼服"
    assert asset.match_keys() == ("sarah", "莎拉")


def test_asset_rejects_blank_name():
    with pytest.raises(ValidationError):
        CharacterAsset(
            asset_id="a1",
            name="   ",
            variant="默认",
            kind=AssetKind.CHARACTER,
            storage_path="asset-library/a1.png",
            storage_url="https://example.com/asset-library/a1.png",
            mime_type="image/png",
            byte_size=1,
        )


def test_asset_rejects_non_image_mime():
    with pytest.raises(ValidationError):
        CharacterAsset(
            asset_id="a1",
            name="Sarah",
            variant="默认",
            kind=AssetKind.CHARACTER,
            storage_path="asset-library/a1.txt",
            storage_url="https://example.com/asset-library/a1.txt",
            mime_type="text/plain",
            byte_size=1,
        )


def test_aliases_deduplicate_and_strip():
    asset = CharacterAsset(
        asset_id="a1",
        name="Sarah",
        variant="默认",
        kind=AssetKind.CHARACTER,
        aliases=["  莎拉 ", "莎拉", "Sarah"],
        storage_path="asset-library/a1.png",
        storage_url="https://example.com/asset-library/a1.png",
        mime_type="image/png",
        byte_size=1,
    )
    assert asset.aliases == ["莎拉"]


def test_normalize_alias_lowercases_ascii_only():
    assert normalize_alias("  Sarah ") == "sarah"
    assert normalize_alias("莎拉") == "莎拉"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd feishu-generation-agent && uv run pytest tests/unit/test_asset_library_domain.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'feishu_generation_agent.domain.asset_library'`

- [ ] **Step 3: 实现领域模型**

创建 `src/feishu_generation_agent/domain/asset_library.py`：

```python
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

    @field_validator("aliases")
    @classmethod
    def drop_alias_equal_to_nothing(cls, value: list[str]) -> list[str]:
        return value

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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd feishu-generation-agent && uv run pytest tests/unit/test_asset_library_domain.py -v`
Expected: PASS（5 passed）

- [ ] **Step 5: Commit**

```bash
git add src/feishu_generation_agent/domain/asset_library.py tests/unit/test_asset_library_domain.py
git commit -m "feat(asset-library): add CharacterAsset domain model"
```

---

## Task 2: 配置项（asset_base_url 等）

**Files:**
- Modify: `src/feishu_generation_agent/config.py`
- Test: `tests/unit/test_asset_library_config.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/unit/test_asset_library_config.py`：

```python
from pathlib import Path

from feishu_generation_agent.config import Settings


def test_asset_library_defaults():
    settings = Settings(_env_file=None)
    assert settings.asset_library_db_path == Path("data/asset-library.sqlite3")
    assert settings.asset_library_dir == Path("data/asset-library")
    assert settings.asset_base_url == "http://127.0.0.1:8765"


def test_asset_base_url_strips_trailing_slash():
    settings = Settings(_env_file=None, asset_base_url="https://media.example.com/")
    assert settings.asset_base_url == "https://media.example.com"


def test_asset_public_url_builds_from_base():
    settings = Settings(_env_file=None, asset_base_url="https://media.example.com")
    assert (
        settings.asset_public_url("asset-library/a1.png")
        == "https://media.example.com/asset-library/a1.png"
    )
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd feishu-generation-agent && uv run pytest tests/unit/test_asset_library_config.py -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'asset_library_db_path'`

- [ ] **Step 3: 加配置字段**

在 `src/feishu_generation_agent/config.py` 的 `Settings` 类里，紧跟 `checkpoint_db_path: Path = Path("data/checkpoints.sqlite3")` 之后插入：

```python
    asset_library_db_path: Path = Path("data/asset-library.sqlite3")
    asset_library_dir: Path = Path("data/asset-library")
    # 服务机 LAN IP 每周变动，禁止在代码里硬编码；部署时通过 .env 覆盖。
    asset_base_url: str = "http://127.0.0.1:8765"
```

在同一文件的 `Settings` 类里，`production_bitable_configured` property 之前插入：

```python
    @field_validator("asset_base_url")
    @classmethod
    def strip_asset_base_url(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        if not normalized:
            raise ValueError("asset_base_url 不能为空")
        return normalized

    def asset_public_url(self, storage_path: str) -> str:
        return f"{self.asset_base_url}/{storage_path.lstrip('/')}"
```

同文件顶部 import 行改为（补 `field_validator`）：

```python
from pydantic import Field, SecretStr, field_validator
```

在 `ensure_paths` 方法的路径元组里补两项，改成：

```python
    def ensure_paths(self) -> None:
        for path in (
            self.data_dir,
            self.outputs_dir,
            self.business_db_path.parent,
            self.checkpoint_db_path.parent,
            self.asset_library_db_path.parent,
            self.asset_library_dir,
        ):
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd feishu-generation-agent && uv run pytest tests/unit/test_asset_library_config.py -v`
Expected: PASS（3 passed）

- [ ] **Step 5: 确认没弄坏已有配置测试**

Run: `cd feishu-generation-agent && uv run pytest tests/unit/test_config.py tests/unit/test_config_probe.py -v`
Expected: PASS（全部通过）

- [ ] **Step 6: 补 .env.example**

在 `.env.example` 末尾追加：

```
# 素材库对外 URL base。服务机 LAN IP 每周会变，部署时按实际填写；
# 改这个值后必须跑 scripts/rewrite_asset_urls.py 重写已有素材的 storage_url。
ASSET_BASE_URL=http://127.0.0.1:8765
```

- [ ] **Step 7: Commit**

```bash
git add src/feishu_generation_agent/config.py tests/unit/test_asset_library_config.py .env.example
git commit -m "feat(asset-library): add configurable asset base url and paths"
```

---

## Task 3: 存储层 AssetLibraryStore（建表 + 新增）

**Files:**
- Create: `src/feishu_generation_agent/storage/asset_library.py`
- Test: `tests/unit/test_asset_library_store.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/unit/test_asset_library_store.py`：

```python
import base64
from pathlib import Path

import pytest

from feishu_generation_agent.domain.asset_library import AssetKind
from feishu_generation_agent.storage.asset_library import (
    AssetLibraryStore,
    DuplicateAssetError,
)


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


@pytest.fixture
async def store(tmp_path: Path):
    opened = await AssetLibraryStore.open(
        db_path=tmp_path / "asset-library.sqlite3",
        assets_dir=tmp_path / "asset-library",
        base_url="https://media.example.com",
    )
    yield opened
    await opened.close()


async def test_create_asset_persists_file_and_row(store, tmp_path):
    asset = await store.create(
        name="Sarah",
        variant="晚宴礼服",
        kind=AssetKind.CHARACTER,
        description="女主",
        aliases=["莎拉"],
        tags=["女主"],
        model_prefs=["seedream"],
        prompt_fragment="金色长发，蓝色眼睛",
        content=PNG_1X1,
        mime_type="image/png",
    )
    assert asset.name == "Sarah"
    assert asset.variant == "晚宴礼服"
    assert asset.byte_size == len(PNG_1X1)
    assert asset.storage_url == (
        f"https://media.example.com/{asset.storage_path}"
    )
    on_disk = tmp_path / asset.storage_path
    assert on_disk.read_bytes() == PNG_1X1

    fetched = await store.get(asset.asset_id)
    assert fetched is not None
    assert fetched.aliases == ["莎拉"]
    assert fetched.prompt_fragment == "金色长发，蓝色眼睛"


async def test_same_name_different_variant_allowed(store):
    await store.create(
        name="Sarah",
        variant="晚宴礼服",
        content=PNG_1X1,
        mime_type="image/png",
    )
    second = await store.create(
        name="Sarah",
        variant="战斗装",
        content=PNG_1X1,
        mime_type="image/png",
    )
    assert second.variant == "战斗装"
    listed = await store.list_all()
    assert len(listed) == 2


async def test_duplicate_name_variant_rejected(store):
    await store.create(
        name="Sarah",
        variant="晚宴礼服",
        content=PNG_1X1,
        mime_type="image/png",
    )
    with pytest.raises(DuplicateAssetError):
        await store.create(
            name="Sarah",
            variant="晚宴礼服",
            content=PNG_1X1,
            mime_type="image/png",
        )


async def test_rejects_non_image_mime(store):
    with pytest.raises(ValueError):
        await store.create(
            name="Sarah",
            variant="默认",
            content=b"not an image",
            mime_type="text/plain",
        )
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd feishu-generation-agent && uv run pytest tests/unit/test_asset_library_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'feishu_generation_agent.storage.asset_library'`

- [ ] **Step 3: 实现存储层（建表 + create + get + list 基础版）**

创建 `src/feishu_generation_agent/storage/asset_library.py`：

```python
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

    async def list_all(
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd feishu-generation-agent && uv run pytest tests/unit/test_asset_library_store.py -v`
Expected: PASS（4 passed）

- [ ] **Step 5: Commit**

```bash
git add src/feishu_generation_agent/storage/asset_library.py tests/unit/test_asset_library_store.py
git commit -m "feat(asset-library): add sqlite store with create/get/list"
```

---

## Task 4: 存储层 update / delete / 按名字匹配

**Files:**
- Modify: `src/feishu_generation_agent/storage/asset_library.py`
- Modify: `tests/unit/test_asset_library_store.py`

- [ ] **Step 1: 追加失败测试**

在 `tests/unit/test_asset_library_store.py` 末尾追加：

```python
async def test_update_metadata_only(store):
    asset = await store.create(
        name="Sarah",
        variant="晚宴礼服",
        content=PNG_1X1,
        mime_type="image/png",
    )
    updated = await store.update(
        asset.asset_id,
        description="改过的描述",
        tags=["女主", "反转"],
        aliases=["莎拉", "Sara"],
        prompt_fragment="金色长发",
    )
    assert updated is not None
    assert updated.description == "改过的描述"
    assert updated.tags == ["女主", "反转"]
    assert updated.aliases == ["莎拉", "Sara"]
    assert updated.updated_at >= asset.updated_at
    assert updated.storage_url == asset.storage_url


async def test_update_rename_to_existing_pair_rejected(store):
    await store.create(
        name="Sarah", variant="晚宴礼服", content=PNG_1X1, mime_type="image/png"
    )
    second = await store.create(
        name="Sarah", variant="战斗装", content=PNG_1X1, mime_type="image/png"
    )
    with pytest.raises(DuplicateAssetError):
        await store.update(second.asset_id, variant="晚宴礼服")


async def test_update_missing_returns_none(store):
    assert await store.update("nope", description="x") is None


async def test_delete_removes_row_and_file(store, tmp_path):
    asset = await store.create(
        name="Sarah", variant="默认", content=PNG_1X1, mime_type="image/png"
    )
    on_disk = tmp_path / asset.storage_path
    assert on_disk.exists()
    assert await store.delete(asset.asset_id) is True
    assert await store.get(asset.asset_id) is None
    assert not on_disk.exists()
    assert await store.delete(asset.asset_id) is False


async def test_find_by_match_key_matches_name_and_alias(store):
    await store.create(
        name="Sarah",
        variant="晚宴礼服",
        aliases=["莎拉"],
        content=PNG_1X1,
        mime_type="image/png",
    )
    await store.create(
        name="Sarah",
        variant="战斗装",
        aliases=["莎拉"],
        content=PNG_1X1,
        mime_type="image/png",
    )
    await store.create(
        name="Victor", variant="默认", content=PNG_1X1, mime_type="image/png"
    )

    by_name = await store.find_by_match_key("sarah")
    assert {item.variant for item in by_name} == {"晚宴礼服", "战斗装"}

    by_alias = await store.find_by_match_key("莎拉")
    assert len(by_alias) == 2

    assert await store.find_by_match_key("不存在") == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd feishu-generation-agent && uv run pytest tests/unit/test_asset_library_store.py -v`
Expected: FAIL — `AttributeError: 'AssetLibraryStore' object has no attribute 'update'`

- [ ] **Step 3: 实现 update / delete / find_by_match_key**

在 `src/feishu_generation_agent/storage/asset_library.py` 的 `list` 方法之后、`_row_to_asset` 之前插入：

```python
    async def update(
        self,
        asset_id: str,
        *,
        name: str | None = None,
        variant: str | None = None,
        kind: AssetKind | None = None,
        description: str | None = None,
        aliases: list[str] | None = None,
        tags: list[str] | None = None,
        model_prefs: list[str] | None = None,
        prompt_fragment: str | None = None,
    ) -> CharacterAsset | None:
        current = await self.get(asset_id)
        if current is None:
            return None

        merged = current.model_copy(
            update={
                "name": current.name if name is None else name,
                "variant": current.variant if variant is None else variant,
                "kind": current.kind if kind is None else kind,
                "description": (
                    current.description if description is None else description
                ),
                "aliases": current.aliases if aliases is None else aliases,
                "tags": current.tags if tags is None else tags,
                "model_prefs": (
                    current.model_prefs if model_prefs is None else model_prefs
                ),
                "prompt_fragment": (
                    current.prompt_fragment
                    if prompt_fragment is None
                    else prompt_fragment
                ),
                "updated_at": datetime.now(timezone.utc),
            }
        )
        revalidated = CharacterAsset.model_validate(merged.model_dump())

        try:
            await self._connection.execute(
                """
                UPDATE asset_library
                   SET name = ?, variant = ?, kind = ?, description = ?,
                       aliases = ?, tags = ?, model_prefs = ?,
                       prompt_fragment = ?, updated_at = ?
                 WHERE asset_id = ?
                """,
                (
                    revalidated.name,
                    revalidated.variant,
                    revalidated.kind.value,
                    revalidated.description,
                    json.dumps(revalidated.aliases, ensure_ascii=False),
                    json.dumps(revalidated.tags, ensure_ascii=False),
                    json.dumps(revalidated.model_prefs, ensure_ascii=False),
                    revalidated.prompt_fragment,
                    revalidated.updated_at.isoformat(),
                    asset_id,
                ),
            )
        except aiosqlite.IntegrityError as error:
            raise DuplicateAssetError(
                f"素材已存在：{revalidated.name} / {revalidated.variant}"
            ) from error
        await self._connection.commit()
        return revalidated

    async def delete(self, asset_id: str) -> bool:
        current = await self.get(asset_id)
        if current is None:
            return False
        await self._connection.execute(
            "DELETE FROM asset_library WHERE asset_id = ?", (asset_id,)
        )
        await self._connection.commit()
        target = self._assets_dir.parent / current.storage_path
        target.unlink(missing_ok=True)
        return True

    async def find_by_match_key(self, key: str) -> list[CharacterAsset]:
        normalized = normalize_alias(key)
        if not normalized:
            return []
        candidates = await self.list_all(limit=500)
        return [
            asset for asset in candidates if normalized in asset.match_keys()
        ]
```

同文件顶部 import 补 `normalize_alias`：

```python
from feishu_generation_agent.domain.asset_library import (
    ASSET_IMAGE_MIME_TYPES,
    AssetKind,
    CharacterAsset,
    normalize_alias,
)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd feishu-generation-agent && uv run pytest tests/unit/test_asset_library_store.py -v`
Expected: PASS（9 passed）

- [ ] **Step 5: Commit**

```bash
git add src/feishu_generation_agent/storage/asset_library.py tests/unit/test_asset_library_store.py
git commit -m "feat(asset-library): add update/delete/find_by_match_key"
```

---

## Task 5: bootstrap 注入 AssetLibraryStore

**Files:**
- Modify: `src/feishu_generation_agent/bootstrap.py`
- Test: `tests/unit/test_asset_library_bootstrap.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/unit/test_asset_library_bootstrap.py`：

```python
from pathlib import Path

from feishu_generation_agent.bootstrap import open_asset_library_store
from feishu_generation_agent.config import Settings


async def test_open_asset_library_store_uses_settings(tmp_path: Path):
    settings = Settings(
        _env_file=None,
        asset_library_db_path=tmp_path / "asset-library.sqlite3",
        asset_library_dir=tmp_path / "asset-library",
        asset_base_url="https://media.example.com",
    )
    store = await open_asset_library_store(settings)
    try:
        asset = await store.create(
            name="Sarah",
            variant="默认",
            content=b"\x89PNG\r\n\x1a\n",
            mime_type="image/png",
        )
        assert asset.storage_url.startswith("https://media.example.com/")
        assert (tmp_path / "asset-library.sqlite3").exists()
    finally:
        await store.close()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd feishu-generation-agent && uv run pytest tests/unit/test_asset_library_bootstrap.py -v`
Expected: FAIL — `ImportError: cannot import name 'open_asset_library_store'`

- [ ] **Step 3: 实现 bootstrap 工厂**

在 `src/feishu_generation_agent/bootstrap.py` 顶部 import 区补：

```python
from feishu_generation_agent.storage.asset_library import AssetLibraryStore
```

在同文件 `def runtime_is_configured(settings: Settings) -> bool:` 之后插入：

```python
async def open_asset_library_store(settings: Settings) -> AssetLibraryStore:
    return await AssetLibraryStore.open(
        db_path=settings.asset_library_db_path,
        assets_dir=settings.asset_library_dir,
        base_url=settings.asset_base_url,
    )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd feishu-generation-agent && uv run pytest tests/unit/test_asset_library_bootstrap.py -v`
Expected: PASS（1 passed）

- [ ] **Step 5: Commit**

```bash
git add src/feishu_generation_agent/bootstrap.py tests/unit/test_asset_library_bootstrap.py
git commit -m "feat(asset-library): wire store factory into bootstrap"
```

---

## Task 6: REST schema

**Files:**
- Modify: `src/feishu_generation_agent/web/schemas.py`
- Test: `tests/unit/test_asset_library_schemas.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/unit/test_asset_library_schemas.py`：

```python
from datetime import datetime, timezone

from feishu_generation_agent.domain.asset_library import AssetKind, CharacterAsset
from feishu_generation_agent.web.schemas import (
    AssetLibraryItem,
    AssetLibraryListResponse,
    AssetLibraryUpdateRequest,
)


def _asset() -> CharacterAsset:
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    return CharacterAsset(
        asset_id="a1",
        name="Sarah",
        variant="晚宴礼服",
        kind=AssetKind.CHARACTER,
        description="女主",
        aliases=["莎拉"],
        tags=["女主"],
        model_prefs=["seedream"],
        prompt_fragment="金色长发",
        storage_path="asset-library/a1.png",
        storage_url="https://media.example.com/asset-library/a1.png",
        mime_type="image/png",
        byte_size=10,
        created_at=now,
        updated_at=now,
    )


def test_item_from_domain_exposes_url_not_path():
    item = AssetLibraryItem.from_domain(_asset())
    assert item.asset_id == "a1"
    assert item.name == "Sarah"
    assert item.variant == "晚宴礼服"
    assert item.url == "https://media.example.com/asset-library/a1.png"
    assert not hasattr(item, "storage_path")


def test_list_response_wraps_items():
    response = AssetLibraryListResponse.from_domain([_asset()])
    assert response.total == 1
    assert response.items[0].name == "Sarah"


def test_update_request_all_fields_optional():
    request = AssetLibraryUpdateRequest()
    assert request.name is None
    assert request.tags is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd feishu-generation-agent && uv run pytest tests/unit/test_asset_library_schemas.py -v`
Expected: FAIL — `ImportError: cannot import name 'AssetLibraryItem'`

- [ ] **Step 3: 实现 schema**

在 `src/feishu_generation_agent/web/schemas.py` 末尾追加：

```python
class AssetLibraryItem(BaseModel):
    asset_id: str
    name: str
    variant: str
    kind: str
    description: str
    aliases: list[str]
    tags: list[str]
    model_prefs: list[str]
    prompt_fragment: str
    url: str
    mime_type: str
    byte_size: int
    created_at: str
    updated_at: str

    @classmethod
    def from_domain(cls, asset: "CharacterAsset") -> "AssetLibraryItem":
        return cls(
            asset_id=asset.asset_id,
            name=asset.name,
            variant=asset.variant,
            kind=asset.kind.value,
            description=asset.description,
            aliases=list(asset.aliases),
            tags=list(asset.tags),
            model_prefs=list(asset.model_prefs),
            prompt_fragment=asset.prompt_fragment,
            url=asset.storage_url,
            mime_type=asset.mime_type,
            byte_size=asset.byte_size,
            created_at=asset.created_at.isoformat(),
            updated_at=asset.updated_at.isoformat(),
        )


class AssetLibraryListResponse(BaseModel):
    items: list[AssetLibraryItem]
    total: int

    @classmethod
    def from_domain(
        cls, assets: list["CharacterAsset"]
    ) -> "AssetLibraryListResponse":
        items = [AssetLibraryItem.from_domain(asset) for asset in assets]
        return cls(items=items, total=len(items))


class AssetLibraryUpdateRequest(BaseModel):
    name: str | None = None
    variant: str | None = None
    kind: str | None = None
    description: str | None = None
    aliases: list[str] | None = None
    tags: list[str] | None = None
    model_prefs: list[str] | None = None
    prompt_fragment: str | None = None
```

同文件顶部 import 区补：

```python
from feishu_generation_agent.domain.asset_library import CharacterAsset
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd feishu-generation-agent && uv run pytest tests/unit/test_asset_library_schemas.py -v`
Expected: PASS（3 passed）

- [ ] **Step 5: Commit**

```bash
git add src/feishu_generation_agent/web/schemas.py tests/unit/test_asset_library_schemas.py
git commit -m "feat(asset-library): add rest schemas"
```

---

## Task 7: REST 路由 + 静态挂载

**Files:**
- Modify: `src/feishu_generation_agent/web/app.py`
- Test: `tests/unit/test_asset_library_api.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/unit/test_asset_library_api.py`：

```python
import base64
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from feishu_generation_agent.storage.asset_library import AssetLibraryStore
from feishu_generation_agent.web.app import register_asset_library_routes


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


@pytest.fixture
async def client(tmp_path: Path):
    store = await AssetLibraryStore.open(
        db_path=tmp_path / "asset-library.sqlite3",
        assets_dir=tmp_path / "asset-library",
        base_url="https://media.example.com",
    )
    app = FastAPI()
    register_asset_library_routes(app, lambda: store)
    with TestClient(app) as test_client:
        yield test_client
    await store.close()


def test_create_and_list_asset(client):
    response = client.post(
        "/api/asset-library/assets",
        data={
            "name": "Sarah",
            "variant": "晚宴礼服",
            "description": "女主",
            "aliases": "莎拉,Sara",
            "tags": "女主,爽剧",
            "model_prefs": "seedream",
            "prompt_fragment": "金色长发",
        },
        files={"file": ("sarah.png", PNG_1X1, "image/png")},
    )
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["name"] == "Sarah"
    assert created["variant"] == "晚宴礼服"
    assert created["aliases"] == ["莎拉", "Sara"]
    assert created["url"].startswith("https://media.example.com/")

    listed = client.get("/api/asset-library/assets")
    assert listed.status_code == 200
    assert listed.json()["total"] == 1


def test_duplicate_returns_409(client):
    payload = {"name": "Sarah", "variant": "晚宴礼服"}
    files = {"file": ("sarah.png", PNG_1X1, "image/png")}
    first = client.post(
        "/api/asset-library/assets", data=payload, files=files
    )
    assert first.status_code == 201
    second = client.post(
        "/api/asset-library/assets",
        data=payload,
        files={"file": ("sarah.png", PNG_1X1, "image/png")},
    )
    assert second.status_code == 409


def test_non_image_returns_400(client):
    response = client.post(
        "/api/asset-library/assets",
        data={"name": "Sarah", "variant": "默认"},
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )
    assert response.status_code == 400


def test_patch_and_delete_asset(client):
    created = client.post(
        "/api/asset-library/assets",
        data={"name": "Sarah", "variant": "晚宴礼服"},
        files={"file": ("sarah.png", PNG_1X1, "image/png")},
    ).json()
    asset_id = created["asset_id"]

    patched = client.patch(
        f"/api/asset-library/assets/{asset_id}",
        json={"description": "改过", "tags": ["反转"]},
    )
    assert patched.status_code == 200
    assert patched.json()["description"] == "改过"
    assert patched.json()["tags"] == ["反转"]

    missing = client.patch(
        "/api/asset-library/assets/nope", json={"description": "x"}
    )
    assert missing.status_code == 404

    deleted = client.delete(f"/api/asset-library/assets/{asset_id}")
    assert deleted.status_code == 204
    assert client.get("/api/asset-library/assets").json()["total"] == 0
    assert (
        client.delete(f"/api/asset-library/assets/{asset_id}").status_code == 404
    )
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd feishu-generation-agent && uv run pytest tests/unit/test_asset_library_api.py -v`
Expected: FAIL — `ImportError: cannot import name 'register_asset_library_routes'`

- [ ] **Step 3: 实现路由注册函数**

在 `src/feishu_generation_agent/web/app.py` 末尾（模块级，不在 `create_app` 内部）追加：

```python
def _split_csv(value: str | None) -> list[str] | None:
    if value is None:
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


def register_asset_library_routes(
    app: FastAPI,
    store_provider: Callable[[], "AssetLibraryStore"],
) -> None:
    @app.post(
        "/api/asset-library/assets",
        response_model=AssetLibraryItem,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_asset_library_item(
        file: Annotated[UploadFile, File()],
        name: Annotated[str, Form()],
        variant: Annotated[str, Form()] = "默认",
        kind: Annotated[str, Form()] = "character",
        description: Annotated[str, Form()] = "",
        aliases: Annotated[str | None, Form()] = None,
        tags: Annotated[str | None, Form()] = None,
        model_prefs: Annotated[str | None, Form()] = None,
        prompt_fragment: Annotated[str, Form()] = "",
    ) -> AssetLibraryItem:
        content = await file.read()
        try:
            asset = await store_provider().create(
                name=name,
                variant=variant,
                kind=AssetKind(kind),
                description=description,
                aliases=_split_csv(aliases),
                tags=_split_csv(tags),
                model_prefs=_split_csv(model_prefs),
                prompt_fragment=prompt_fragment,
                content=content,
                mime_type=file.content_type or "",
            )
        except DuplicateAssetError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(error)
            ) from error
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)
            ) from error
        return AssetLibraryItem.from_domain(asset)

    @app.get(
        "/api/asset-library/assets",
        response_model=AssetLibraryListResponse,
    )
    async def list_asset_library_items(
        name: str | None = None,
        kind: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> AssetLibraryListResponse:
        assets = await store_provider().list_all(
            kind=AssetKind(kind) if kind else None,
            name=name,
            limit=limit,
            offset=offset,
        )
        return AssetLibraryListResponse.from_domain(assets)

    @app.patch(
        "/api/asset-library/assets/{asset_id}",
        response_model=AssetLibraryItem,
    )
    async def update_asset_library_item(
        asset_id: str,
        payload: AssetLibraryUpdateRequest,
    ) -> AssetLibraryItem:
        try:
            asset = await store_provider().update(
                asset_id,
                name=payload.name,
                variant=payload.variant,
                kind=AssetKind(payload.kind) if payload.kind else None,
                description=payload.description,
                aliases=payload.aliases,
                tags=payload.tags,
                model_prefs=payload.model_prefs,
                prompt_fragment=payload.prompt_fragment,
            )
        except DuplicateAssetError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(error)
            ) from error
        if asset is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="素材不存在"
            )
        return AssetLibraryItem.from_domain(asset)

    @app.delete(
        "/api/asset-library/assets/{asset_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def delete_asset_library_item(asset_id: str) -> None:
        if not await store_provider().delete(asset_id):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="素材不存在"
            )
```

同文件顶部 import 区补：

```python
from collections.abc import Callable

from feishu_generation_agent.domain.asset_library import AssetKind
from feishu_generation_agent.storage.asset_library import (
    AssetLibraryStore,
    DuplicateAssetError,
)
from feishu_generation_agent.web.schemas import (
    AssetLibraryItem,
    AssetLibraryListResponse,
    AssetLibraryUpdateRequest,
)
```

注意：`web/schemas.py` 的 import 是现有的一个多行 `from ... import (...)` 块，把这三个名字加进去即可，不要新开一个 import 语句。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd feishu-generation-agent && uv run pytest tests/unit/test_asset_library_api.py -v`
Expected: PASS（4 passed）

- [ ] **Step 5: Commit**

```bash
git add src/feishu_generation_agent/web/app.py tests/unit/test_asset_library_api.py
git commit -m "feat(asset-library): add rest routes"
```

---

## Task 8: 接进 create_app（含静态挂载与生命周期）

**Files:**
- Modify: `src/feishu_generation_agent/web/app.py`
- Test: `tests/unit/test_asset_library_app_wiring.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/unit/test_asset_library_app_wiring.py`：

```python
from pathlib import Path

from fastapi.testclient import TestClient

from feishu_generation_agent.config import Settings
from feishu_generation_agent.web.app import create_app


def test_asset_library_static_serves_uploaded_file(tmp_path: Path):
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        outputs_dir=tmp_path / "outputs",
        business_db_path=tmp_path / "data" / "agent.sqlite3",
        checkpoint_db_path=tmp_path / "data" / "checkpoints.sqlite3",
        asset_library_db_path=tmp_path / "data" / "asset-library.sqlite3",
        asset_library_dir=tmp_path / "data" / "asset-library",
        asset_base_url="https://media.example.com",
    )
    settings.ensure_paths()
    app = create_app(settings)
    with TestClient(app) as client:
        created = client.post(
            "/api/asset-library/assets",
            data={"name": "Sarah", "variant": "默认"},
            files={"file": ("sarah.png", b"\x89PNG\r\n\x1a\n", "image/png")},
        )
        assert created.status_code == 201, created.text
        relative = created.json()["url"].removeprefix(
            "https://media.example.com"
        )
        served = client.get(relative)
        assert served.status_code == 200
        assert served.content == b"\x89PNG\r\n\x1a\n"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd feishu-generation-agent && uv run pytest tests/unit/test_asset_library_app_wiring.py -v`
Expected: FAIL — 404，因为 `create_app` 还没注册素材库路由和静态挂载

- [ ] **Step 3: 在 create_app 里接线**

在 `src/feishu_generation_agent/web/app.py` 中找到 `app.mount("/static", StaticFiles(directory=static_dir), name="static")` 这一行（约 1023 行），在它**之前**插入：

```python
    asset_library_holder: dict[str, AssetLibraryStore] = {}

    @app.on_event("startup")
    async def _open_asset_library() -> None:
        asset_library_holder["store"] = await AssetLibraryStore.open(
            db_path=settings.asset_library_db_path,
            assets_dir=settings.asset_library_dir,
            base_url=settings.asset_base_url,
        )

    @app.on_event("shutdown")
    async def _close_asset_library() -> None:
        store = asset_library_holder.pop("store", None)
        if store is not None:
            await store.close()

    register_asset_library_routes(
        app, lambda: asset_library_holder["store"]
    )
    settings.asset_library_dir.mkdir(parents=True, exist_ok=True)
    app.mount(
        f"/{settings.asset_library_dir.name}",
        StaticFiles(directory=settings.asset_library_dir),
        name="asset-library",
    )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd feishu-generation-agent && uv run pytest tests/unit/test_asset_library_app_wiring.py -v`
Expected: PASS（1 passed）

- [ ] **Step 5: 跑全量测试确认没弄坏别的**

Run: `cd feishu-generation-agent && uv run pytest -q`
Expected: 全部 PASS

- [ ] **Step 6: Commit**

```bash
git add src/feishu_generation_agent/web/app.py tests/unit/test_asset_library_app_wiring.py
git commit -m "feat(asset-library): mount routes and static files in app"
```

---

## Task 9: URL base 迁移脚本

**Files:**
- Create: `scripts/rewrite_asset_urls.py`
- Test: `tests/unit/test_asset_library_url_migration.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/unit/test_asset_library_url_migration.py`：

```python
from pathlib import Path

import pytest

from feishu_generation_agent.storage.asset_library import (
    AssetLibraryStore,
    rewrite_storage_urls,
)


@pytest.fixture
async def store(tmp_path: Path):
    opened = await AssetLibraryStore.open(
        db_path=tmp_path / "asset-library.sqlite3",
        assets_dir=tmp_path / "asset-library",
        base_url="https://old.example.com",
    )
    yield opened
    await opened.close()


async def test_rewrite_storage_urls_updates_all_rows(store):
    first = await store.create(
        name="Sarah", variant="默认", content=b"\x89PNG", mime_type="image/png"
    )
    second = await store.create(
        name="Victor", variant="默认", content=b"\x89PNG", mime_type="image/png"
    )
    assert first.storage_url.startswith("https://old.example.com/")

    changed = await rewrite_storage_urls(store, "https://new.example.com/")
    assert changed == 2

    refreshed_first = await store.get(first.asset_id)
    refreshed_second = await store.get(second.asset_id)
    assert refreshed_first is not None and refreshed_second is not None
    assert refreshed_first.storage_url == (
        f"https://new.example.com/{first.storage_path}"
    )
    assert refreshed_second.storage_url == (
        f"https://new.example.com/{second.storage_path}"
    )


async def test_rewrite_is_idempotent(store):
    await store.create(
        name="Sarah", variant="默认", content=b"\x89PNG", mime_type="image/png"
    )
    assert await rewrite_storage_urls(store, "https://new.example.com") == 1
    assert await rewrite_storage_urls(store, "https://new.example.com") == 0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd feishu-generation-agent && uv run pytest tests/unit/test_asset_library_url_migration.py -v`
Expected: FAIL — `ImportError: cannot import name 'rewrite_storage_urls'`

- [ ] **Step 3: 实现 rewrite_storage_urls**

在 `src/feishu_generation_agent/storage/asset_library.py` 末尾（模块级函数，不在类内）追加：

```python
async def rewrite_storage_urls(
    store: "AssetLibraryStore", new_base_url: str
) -> int:
    base = new_base_url.rstrip("/")
    if not base:
        raise ValueError("new_base_url 不能为空")
    connection = store._connection  # noqa: SLF001 — 迁移脚本专用
    cursor = await connection.execute(
        "SELECT asset_id, storage_path, storage_url FROM asset_library"
    )
    rows = await cursor.fetchall()
    await cursor.close()

    changed = 0
    for row in rows:
        expected = f"{base}/{row['storage_path']}"
        if row["storage_url"] == expected:
            continue
        await connection.execute(
            "UPDATE asset_library SET storage_url = ? WHERE asset_id = ?",
            (expected, row["asset_id"]),
        )
        changed += 1
    if changed:
        await connection.commit()
    store._base_url = base  # noqa: SLF001 — 让同进程后续写入用新 base
    return changed
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd feishu-generation-agent && uv run pytest tests/unit/test_asset_library_url_migration.py -v`
Expected: PASS（2 passed）

- [ ] **Step 5: 写 CLI 脚本**

创建 `scripts/rewrite_asset_urls.py`：

```python
"""换 ASSET_BASE_URL 后批量重写素材库 storage_url。

用法：
    cd feishu-generation-agent
    uv run python scripts/rewrite_asset_urls.py            # 用 .env 里的 ASSET_BASE_URL
    uv run python scripts/rewrite_asset_urls.py https://new.example.com

服务机 LAN IP 每周会变，改完 .env 必须跑一次这个脚本。
"""

import asyncio
import sys

from feishu_generation_agent.config import Settings
from feishu_generation_agent.storage.asset_library import (
    AssetLibraryStore,
    rewrite_storage_urls,
)


async def main() -> None:
    settings = Settings()
    target = sys.argv[1] if len(sys.argv) > 1 else settings.asset_base_url
    store = await AssetLibraryStore.open(
        db_path=settings.asset_library_db_path,
        assets_dir=settings.asset_library_dir,
        base_url=settings.asset_base_url,
    )
    try:
        changed = await rewrite_storage_urls(store, target)
    finally:
        await store.close()
    print(f"rewritten={changed} base={target.rstrip('/')}")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 6: 手动验证脚本能跑**

Run: `cd feishu-generation-agent && uv run python scripts/rewrite_asset_urls.py http://127.0.0.1:8765`
Expected: 输出形如 `rewritten=0 base=http://127.0.0.1:8765`（库为空时 0）

- [ ] **Step 7: Commit**

```bash
git add src/feishu_generation_agent/storage/asset_library.py scripts/rewrite_asset_urls.py tests/unit/test_asset_library_url_migration.py
git commit -m "feat(asset-library): add storage url rewrite migration"
```

---

## Task 10: 阶段 1 收尾验证

**Files:** 无新增，只跑验证

- [ ] **Step 1: 全量测试**

Run: `cd feishu-generation-agent && uv run pytest -q`
Expected: 全部 PASS，无 warning 级 error

- [ ] **Step 2: 起服务做一次真实 HTTP 往返**

Run（终端 A）：`cd feishu-generation-agent && uv run feishu-generation-agent`

Run（终端 B）：

```bash
curl -sS -X POST http://127.0.0.1:8765/api/asset-library/assets \
  -F 'name=Sarah' -F 'variant=晚宴礼服' -F 'aliases=莎拉' \
  -F 'tags=女主' -F 'model_prefs=seedream' \
  -F 'file=@/path/to/any.png;type=image/png'
```

Expected: 201，返回体含 `"url": "http://127.0.0.1:8765/asset-library/<uuid>.png"`

再验证 URL 真的能读：

```bash
curl -sS -o /dev/null -w '%{http_code}\n' \
  "$(curl -sS http://127.0.0.1:8765/api/asset-library/assets | python3 -c 'import json,sys; print(json.load(sys.stdin)["items"][0]["url"])')"
```

Expected: `200`

- [ ] **Step 3: 清理验证数据**

```bash
curl -sS http://127.0.0.1:8765/api/asset-library/assets \
  | python3 -c 'import json,sys; [print(i["asset_id"]) for i in json.load(sys.stdin)["items"]]' \
  | xargs -I{} curl -sS -X DELETE http://127.0.0.1:8765/api/asset-library/assets/{}
```

- [ ] **Step 4: Commit（若有 .gitignore 需要补）**

确认 `feishu-generation-agent/.gitignore` 含 `data/`（素材文件和 SQLite 都在 data 下，不应提交）。若缺则补上并：

```bash
git add .gitignore
git commit -m "chore(asset-library): ignore local asset storage"
```

---

## 阶段 1 完成标准

- [ ] `uv run pytest -q` 全绿
- [ ] REST 五个操作可用：POST 上传、GET 列表、GET 单个（通过列表返回的 url）、PATCH 改元数据、DELETE 删除
- [ ] 素材图片能被外部 HTTP 客户端通过 `storage_url` 读到
- [ ] 同一人物不同 variant 能分别入库；同 name+variant 重复被 409 拒绝
- [ ] 代码里搜不到硬编码 `192.168.30.5`：`grep -rn "192\.168\." src/ scripts/` 无结果
- [ ] `scripts/rewrite_asset_urls.py` 能把已有素材的 URL 批量换 base

## 阶段 2/3 会用到的接口（本阶段已就位，勿改签名）

- `AssetLibraryStore.find_by_match_key(key: str) -> list[CharacterAsset]` — 阶段 3 文档人名匹配的入口
- `CharacterAsset.match_keys() -> tuple[str, ...]` — name + aliases 的归一化键
- `CharacterAsset.storage_url` — 阶段 2 图片 provider 直接消费的公网 URL
- `CharacterAsset.volcengine_asset_id` — 阶段 3 火山镜像缓存写入位
- `CharacterAsset.prompt_fragment` — 阶段 2 planner 注入角色描述用
