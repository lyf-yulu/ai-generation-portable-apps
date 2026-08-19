from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from feishu_generation_agent.domain.asset_library import AssetKind, CharacterAsset
from feishu_generation_agent.domain.document import (
    DocumentBlock,
    MediaAsset,
    NormalizedDocument,
    SourceType,
)
from feishu_generation_agent.graph.nodes import _auto_ingest_characters
from feishu_generation_agent.integrations.character_semantic_matcher import (
    UnresolvedCandidate,
)
from feishu_generation_agent.storage.asset_library import DuplicateAssetError


PNG = b"\x89PNG\r\n\x1a\n"


def _document(tmp_path: Path) -> NormalizedDocument:
    image = tmp_path / "mike.png"
    image.write_bytes(PNG)
    return NormalizedDocument(
        document_id="doc-cg",
        title="CG 需求",
        revision=1,
        source_type=SourceType.WIKI,
        source_token="token-cg",
        blocks=[
            DocumentBlock(
                block_id="b1",
                parent_id=None,
                block_type="text",
                order=0,
                path=["b1"],
                text="新角色 Mike 登场，穿着黑色风衣",
            ),
            DocumentBlock(
                block_id="b2",
                parent_id=None,
                block_type="image",
                order=1,
                path=["b2"],
                text="",
                image_asset_id="image-1",
            ),
        ],
        text_view="[block:b1] 新角色 Mike 登场\n[image:image-1]",
        media_assets=[
            MediaAsset(
                asset_id="image-1",
                source_block_id="b2",
                origin="feishu",
                local_path=image,
                mime_type="image/png",
                size=len(PNG),
                sha256="a" * 64,
            )
        ],
    )


class _Store:
    def __init__(self, fail_with: Exception | None = None) -> None:
        self.created: list[dict[str, Any]] = []
        self.fail_with = fail_with

    async def create(self, **kwargs: Any) -> CharacterAsset:
        if self.fail_with is not None:
            raise self.fail_with
        self.created.append(kwargs)
        now = datetime(2026, 8, 12, tzinfo=timezone.utc)
        return CharacterAsset(
            asset_id=f"new-{len(self.created)}",
            name=kwargs["name"],
            variant=kwargs["variant"],
            kind=kwargs.get("kind", AssetKind.CHARACTER),
            description=kwargs.get("description", ""),
            tags=kwargs.get("tags") or [],
            storage_path="asset-library/new.png",
            storage_url="https://media.example.com/asset-library/new.png",
            mime_type="image/png",
            byte_size=len(PNG),
            created_at=now,
            updated_at=now,
        )


def _candidate(name: str = "Mike", block_ids: tuple[str, ...] = ("b1",)):
    return UnresolvedCandidate(
        proposed_name=name, block_ids=block_ids, reason="素材库缺该角色"
    )


async def test_creates_asset_for_unresolved_candidate(tmp_path: Path):
    store = _Store()

    created = await _auto_ingest_characters(
        _document(tmp_path), [_candidate()], store
    )

    assert [item.name for item in created] == ["Mike"]
    assert store.created[0]["name"] == "Mike"
    assert store.created[0]["content"] == PNG


async def test_auto_ingested_assets_are_tagged_for_review(tmp_path: Path):
    """自动入库的条目要能被人工找出来复核。"""
    store = _Store()

    await _auto_ingest_characters(_document(tmp_path), [_candidate()], store)

    tags = store.created[0]["tags"]
    assert "auto-ingested" in tags
    assert any("doc-cg" in tag for tag in tags)


async def test_default_variant_is_used(tmp_path: Path):
    """同人不同着装靠 variant 区分；自动入库先用默认，人工再改。"""
    store = _Store()

    await _auto_ingest_characters(_document(tmp_path), [_candidate()], store)

    assert store.created[0]["variant"] == "默认"


async def test_candidate_without_nearby_image_is_skipped(tmp_path: Path):
    store = _Store()
    document = _document(tmp_path)
    document = document.model_copy(update={"media_assets": []})

    created = await _auto_ingest_characters(document, [_candidate()], store)

    assert created == []
    assert store.created == []


async def test_duplicate_is_not_fatal(tmp_path: Path):
    """重名冲突只跳过该候选，不影响其它候选与整个 run。"""
    store = _Store(fail_with=DuplicateAssetError("已存在"))

    created = await _auto_ingest_characters(
        _document(tmp_path), [_candidate()], store
    )

    assert created == []


async def test_store_failure_is_not_fatal(tmp_path: Path):
    store = _Store(fail_with=RuntimeError("sqlite locked"))

    created = await _auto_ingest_characters(
        _document(tmp_path), [_candidate()], store
    )

    assert created == []


async def test_no_candidates_skips_store(tmp_path: Path):
    store = _Store()

    created = await _auto_ingest_characters(_document(tmp_path), [], store)

    assert created == []
    assert store.created == []


async def test_missing_store_is_safe(tmp_path: Path):
    created = await _auto_ingest_characters(
        _document(tmp_path), [_candidate()], None
    )

    assert created == []


async def test_blank_name_candidate_is_skipped(tmp_path: Path):
    store = _Store()

    created = await _auto_ingest_characters(
        _document(tmp_path), [_candidate(name="   ")], store
    )

    assert created == []
    assert store.created == []
