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
