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
