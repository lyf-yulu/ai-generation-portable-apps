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
