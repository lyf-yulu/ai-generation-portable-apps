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
