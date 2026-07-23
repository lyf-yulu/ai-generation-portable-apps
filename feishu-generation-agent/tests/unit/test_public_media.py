import httpx
import pytest

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
