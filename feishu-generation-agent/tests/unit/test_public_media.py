import httpx
import pytest

from feishu_generation_agent.integrations import public_media
from feishu_generation_agent.integrations.public_media import (
    PublicMediaUploadError,
    UguuPublicMediaHost,
)


@pytest.mark.asyncio
async def test_uguu_host_returns_https_url() -> None:
    async def respond(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("https://uguu.se/upload.php")
        assert request.method == "POST"
        return httpx.Response(
            200, json={"files": [{"url": "https://a.uguu.se/token.mp4"}]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await UguuPublicMediaHost(client).upload(
            b"video", "clip.mp4", "video/mp4"
        )

    assert result == "https://a.uguu.se/token.mp4"


@pytest.mark.asyncio
async def test_uguu_host_rejects_non_https_url() -> None:
    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"files": [{"url": "http://bad.example/a"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(PublicMediaUploadError, match="HTTPS"):
            await UguuPublicMediaHost(client).upload(
                b"audio", "a.mp3", "audio/mpeg"
            )


@pytest.mark.asyncio
async def test_tos_host_uploads_with_sigv4_and_returns_presigned_https_url() -> None:
    requests: list[httpx.Request] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    host_class = getattr(public_media, "TosPublicMediaHost", None)
    assert host_class is not None, "TOS media host is not implemented"
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        host = host_class(
            client,
            access_key="fictional-access-key",
            secret_key="fictional-secret-key",
            bucket="seedance-fixture",
            region="cn-beijing",
        )
        result = await host.upload(b"image-bytes", "dog.png", "image/png")

    assert len(requests) == 1
    request = requests[0]
    assert request.method == "PUT"
    assert request.url.host == "seedance-fixture.tos-cn-beijing.volces.com"
    assert request.url.path.startswith("/refmedia/")
    assert request.url.path.endswith(".png")
    assert request.headers["content-type"] == "image/png"
    assert request.headers["authorization"].startswith(
        "TOS4-HMAC-SHA256 Credential=fictional-access-key/"
    )
    assert "fictional-secret-key" not in request.headers["authorization"]
    assert request.content == b"image-bytes"
    assert result.startswith(
        "https://seedance-fixture.tos-cn-beijing.volces.com/refmedia/"
    )
    assert "X-Tos-Signature=" in result
