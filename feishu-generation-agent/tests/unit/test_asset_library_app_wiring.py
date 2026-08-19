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
    app = create_app(settings=settings)
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
