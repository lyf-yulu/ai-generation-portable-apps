import json
from hashlib import sha256
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from PIL import Image

from feishu_generation_agent.domain.document import MediaAsset
from feishu_generation_agent.domain.errors import AgentError, ErrorCategory
from feishu_generation_agent.integrations.public_media import PublicMediaUploadError
from feishu_generation_agent.integrations.volcengine_portrait import (
    VolcengineAssetClient,
)
from feishu_generation_agent.storage.portrait_assets import PortraitAssetStore


class _PublicMediaHost:
    async def upload(self, content: bytes, filename: str, mime_type: str) -> str:
        with Image.open(BytesIO(content)) as image:
            assert image.size == (300, 300)
        assert filename == "portrait.png"
        assert mime_type == "image/png"
        return "https://public.example/portrait.png"


class _FailingPublicMediaHost:
    async def upload(self, content: bytes, filename: str, mime_type: str) -> str:
        del content, filename, mime_type
        raise PublicMediaUploadError("temporary host unavailable")


class _CapturingPublicMediaHost:
    def __init__(self) -> None:
        self.uploaded: list[tuple[bytes, str, str]] = []

    async def upload(self, content: bytes, filename: str, mime_type: str) -> str:
        self.uploaded.append((content, filename, mime_type))
        return "https://public.example/portrait.png"


def _image_asset(tmp_path: Path) -> MediaAsset:
    path = tmp_path / "portrait.png"
    Image.new("RGB", (300, 300), color=(20, 120, 60)).save(path, format="PNG")
    content = path.read_bytes()
    return MediaAsset(
        asset_id="source-image-1",
        source_block_id="block-1",
        origin="fixture",
        local_path=path,
        mime_type="image/png",
        size=len(content),
        sha256=sha256(content).hexdigest(),
        width=300,
        height=300,
    )


def _png_asset(tmp_path: Path, size: tuple[int, int]) -> MediaAsset:
    path = tmp_path / f"portrait-{size[0]}x{size[1]}.png"
    image = Image.new("RGB", size, color=(20, 120, 60))
    image.save(path, format="PNG")
    content = path.read_bytes()
    return MediaAsset(
        asset_id=f"source-image-{size[0]}x{size[1]}",
        source_block_id="block-1",
        origin="fixture",
        local_path=path,
        mime_type="image/png",
        size=len(content),
        sha256=sha256(content).hexdigest(),
        width=size[0],
        height=size[1],
    )


def _active_asset_handler(request: httpx.Request) -> httpx.Response:
    action = request.url.params["Action"]
    if action == "CreateAssetGroup":
        return httpx.Response(200, json={"Result": {"Id": "group-1"}})
    if action == "CreateAsset":
        return httpx.Response(200, json={"Result": {"Id": "asset-1"}})
    if action == "GetAsset":
        return httpx.Response(200, json={"Result": {"Status": "Active"}})
    raise AssertionError(f"unexpected action: {action}")


async def test_portrait_client_resizes_only_upload_copy_for_small_image(
    tmp_path: Path,
) -> None:
    asset = _png_asset(tmp_path, (216, 384))
    source_before = asset.local_path.read_bytes()
    host = _CapturingPublicMediaHost()
    store = await PortraitAssetStore.open(tmp_path / "portrait.sqlite3")
    try:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(_active_asset_handler)
        ) as http:
            client = VolcengineAssetClient(
                http,
                access_key="ak-test",
                secret_key="sk-test",
                project_name="Seedance2.0",
                public_media_host=host,
                store=store,
                poll_interval_seconds=0,
                max_poll_attempts=1,
            )
            await client.ensure_image_asset("run-small", asset)
    finally:
        await store.close()

    uploaded, filename, mime_type = host.uploaded[0]
    with Image.open(BytesIO(uploaded)) as image:
        assert image.size == (300, 534)
    assert filename == asset.local_path.name
    assert mime_type == "image/png"
    assert asset.local_path.read_bytes() == source_before
    assert sha256(asset.local_path.read_bytes()).hexdigest() == asset.sha256


async def test_portrait_client_keeps_compliant_image_bytes_unchanged(
    tmp_path: Path,
) -> None:
    asset = _png_asset(tmp_path, (720, 1280))
    source = asset.local_path.read_bytes()
    host = _CapturingPublicMediaHost()
    store = await PortraitAssetStore.open(tmp_path / "portrait.sqlite3")
    try:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(_active_asset_handler)
        ) as http:
            client = VolcengineAssetClient(
                http,
                access_key="ak-test",
                secret_key="sk-test",
                project_name="Seedance2.0",
                public_media_host=host,
                store=store,
                poll_interval_seconds=0,
                max_poll_attempts=1,
            )
            await client.ensure_image_asset("run-compliant", asset)
    finally:
        await store.close()

    assert host.uploaded[0][0] == source


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


async def test_portrait_client_reports_public_host_failure_as_transient(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["Action"] == "CreateAssetGroup"
        return httpx.Response(200, json={"Result": {"Id": "group-1"}})

    store = await PortraitAssetStore.open(tmp_path / "portrait.sqlite3")
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = VolcengineAssetClient(
                http,
                access_key="ak-test",
                secret_key="sk-test",
                project_name="Seedance2.0",
                public_media_host=_FailingPublicMediaHost(),
                store=store,
            )
            with pytest.raises(AgentError) as caught:
                await client.ensure_image_asset("run-1", _image_asset(tmp_path))
    finally:
        await store.close()

    assert caught.value.detail.category is ErrorCategory.TRANSIENT
    assert caught.value.detail.retryable is True
