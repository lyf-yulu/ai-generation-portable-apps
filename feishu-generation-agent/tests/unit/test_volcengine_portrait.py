import json
from hashlib import sha256
from pathlib import Path

import httpx

from feishu_generation_agent.domain.document import MediaAsset
from feishu_generation_agent.integrations.volcengine_portrait import (
    VolcengineAssetClient,
)
from feishu_generation_agent.storage.portrait_assets import PortraitAssetStore


class _PublicMediaHost:
    async def upload(self, content: bytes, filename: str, mime_type: str) -> str:
        assert content == b"portrait-image"
        assert filename == "portrait.png"
        assert mime_type == "image/png"
        return "https://public.example/portrait.png"


def _image_asset(tmp_path: Path) -> MediaAsset:
    content = b"portrait-image"
    path = tmp_path / "portrait.png"
    path.write_bytes(content)
    return MediaAsset(
        asset_id="source-image-1",
        source_block_id="block-1",
        origin="fixture",
        local_path=path,
        mime_type="image/png",
        size=len(content),
        sha256=sha256(content).hexdigest(),
        width=1,
        height=1,
    )


async def test_portrait_client_creates_group_activates_image_and_reuses_asset(
    tmp_path: Path,
) -> None:
    actions: list[str] = []
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        action = request.url.params["Action"]
        actions.append(action)
        if action == "CreateAssetGroup":
            return httpx.Response(200, json={"Result": {"Id": "group-1"}})
        if action == "CreateAsset":
            return httpx.Response(200, json={"Result": {"Id": "asset-1"}})
        if action == "GetAsset":
            return httpx.Response(200, json={"Result": {"Status": "Active"}})
        raise AssertionError(f"unexpected action: {action}")

    store = await PortraitAssetStore.open(tmp_path / "portrait.sqlite3")
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = VolcengineAssetClient(
                http,
                access_key="ak-test",
                secret_key="sk-test",
                project_name="Seedance2.0",
                public_media_host=_PublicMediaHost(),
                store=store,
                poll_interval_seconds=0,
                max_poll_attempts=1,
            )
            first = await client.ensure_image_asset("run-1", _image_asset(tmp_path))
            second = await client.ensure_image_asset("run-1", _image_asset(tmp_path))
    finally:
        await store.close()

    assert first == "asset://asset-1"
    assert second == "asset://asset-1"
    assert actions == ["CreateAssetGroup", "CreateAsset", "GetAsset"]
    assert requests[0].headers["authorization"].startswith(
        "HMAC-SHA256 Credential=ak-test/"
    )
    assert "sk-test" not in requests[0].headers["authorization"]
    assert json.loads(requests[1].content)["URL"] == "https://public.example/portrait.png"
