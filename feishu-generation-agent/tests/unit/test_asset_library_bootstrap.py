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
