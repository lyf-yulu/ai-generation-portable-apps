from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from feishu_generation_agent.domain.asset_library import AssetKind, CharacterAsset
from feishu_generation_agent.domain.document import (
    DocumentBlock,
    MediaAsset,
    NormalizedDocument,
    SourceType,
)
from feishu_generation_agent.domain.plan import GenerationTask
from feishu_generation_agent.graph.nodes import (
    _character_media_assets,
    _task_assets,
)
from feishu_generation_agent.storage.asset_library import AssetLibraryStore


PNG = b"\x89PNG\r\n\x1a\n"


@pytest.fixture
async def store(tmp_path: Path):
    opened = await AssetLibraryStore.open(
        db_path=tmp_path / "asset-library.sqlite3",
        assets_dir=tmp_path / "asset-library",
        base_url="https://media.example.com",
    )
    yield opened
    await opened.close()


def _document(assets: list[MediaAsset]) -> NormalizedDocument:
    return NormalizedDocument(
        document_id="doc-1",
        title="CG 需求",
        revision=1,
        source_type=SourceType.WIKI,
        source_token="token-1",
        blocks=[
            DocumentBlock(
                block_id="b1",
                parent_id=None,
                block_type="text",
                order=0,
                path=["b1"],
                text="Sarah 中景",
            )
        ],
        text_view="[block:b1] Sarah 中景",
        media_assets=assets,
    )


def _task(asset_id: str) -> GenerationTask:
    return GenerationTask.model_validate(
        {
            "task_id": "cg-1",
            "task_type": "image_to_image",
            "title": "Sarah",
            "source_block_ids": ["b1"],
            "user_intent": "出图",
            "prompt": "@图片1 中的女性，戏剧化顶光",
            "reference_images": [
                {"asset_id": asset_id, "role": "reference_image", "order": 1}
            ],
            "aspect_ratio": "9:16",
            "image_size": "2K",
        }
    )


async def test_character_asset_becomes_usable_media_asset(store, tmp_path):
    created = await store.create(
        name="Sarah", variant="晚宴礼服", content=PNG, mime_type="image/png"
    )

    media = await _character_media_assets([created], store)

    assert len(media) == 1
    assert media[0].asset_id == created.asset_id
    assert media[0].mime_type == "image/png"
    assert media[0].size == len(PNG)
    assert media[0].local_path.read_bytes() == PNG
    assert media[0].sha256


async def test_provider_can_resolve_library_reference(store):
    """素材库参考图必须能被 _task_assets 解析，否则出图时直接报计划无效。"""
    created = await store.create(
        name="Sarah", variant="晚宴礼服", content=PNG, mime_type="image/png"
    )
    media = await _character_media_assets([created], store)
    document = _document(media)

    resolved = _task_assets(_task(created.asset_id), document)

    assert [item.asset_id for item in resolved] == [created.asset_id]


async def test_missing_file_is_skipped(store):
    created = await store.create(
        name="Sarah", variant="默认", content=PNG, mime_type="image/png"
    )
    store_path = store._assets_dir.parent / created.storage_path
    store_path.unlink()

    media = await _character_media_assets([created], store)

    assert media == []


async def test_no_store_yields_nothing():
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    asset = CharacterAsset(
        asset_id="a1",
        name="Sarah",
        variant="默认",
        kind=AssetKind.CHARACTER,
        storage_path="asset-library/a1.png",
        storage_url="https://media.example.com/asset-library/a1.png",
        mime_type="image/png",
        byte_size=8,
        created_at=now,
        updated_at=now,
    )

    assert await _character_media_assets([asset], None) == []


async def test_empty_input_is_safe(store):
    assert await _character_media_assets([], store) == []
