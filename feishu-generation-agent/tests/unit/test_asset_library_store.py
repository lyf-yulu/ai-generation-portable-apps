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
    listed = await store.list()
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
