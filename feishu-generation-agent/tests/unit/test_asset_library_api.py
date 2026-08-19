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
