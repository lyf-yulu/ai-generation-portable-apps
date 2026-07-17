from collections.abc import Sequence

import httpx
import pytest

from feishu_generation_agent.domain.errors import AgentError, ErrorCategory
from feishu_generation_agent.integrations.safe_download import SafeResultDownloader


async def _public_resolver(host: str, port: int) -> Sequence[str]:
    del host, port
    return ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "https://localhost/result.png",
        "https://images.localhost/result.png",
        "https://127.0.0.1/result.png",
        "https://10.0.0.1/result.png",
        "https://169.254.1.1/result.png",
        "https://224.0.0.1/result.png",
        "https://0.0.0.0/result.png",
        "https://[::1]/result.png",
        "https://[fc00::1]/result.png",
        "https://[fe80::1]/result.png",
        "https://[ff02::1]/result.png",
        "https://[::]/result.png",
        "https://user@public.example/result.png",
        "https://public.example:444/result.png",
        "https://public.example:invalid/result.png",
        "https://public.example:70000/result.png",
    ],
)
async def test_downloader_rejects_local_private_and_malformed_urls_without_http(
    url: str,
) -> None:
    requests: list[httpx.Request] = []

    def unexpected(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(unexpected)
    ) as client:
        downloader = SafeResultDownloader(
            client,
            resolver=_public_resolver,
            max_bytes=1024,
        )
        with pytest.raises(AgentError) as caught:
            await downloader.download(url, expected_mime_type="image/png")

    assert caught.value.detail.category == ErrorCategory.PROVIDER_TERMINAL
    assert caught.value.detail.retryable is False
    assert requests == []


@pytest.mark.asyncio
async def test_downloader_rejects_any_private_dns_answer_without_http() -> None:
    requests: list[httpx.Request] = []

    async def mixed_resolver(host: str, port: int) -> Sequence[str]:
        assert (host, port) == ("mixed.example", 443)
        return ["93.184.216.34", "192.168.1.20"]

    def unexpected(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(unexpected)
    ) as client:
        downloader = SafeResultDownloader(
            client,
            resolver=mixed_resolver,
            max_bytes=1024,
        )
        with pytest.raises(AgentError) as caught:
            await downloader.download(
                "https://mixed.example/result.png",
                expected_mime_type="image/png",
            )

    assert caught.value.detail.category == ErrorCategory.PROVIDER_TERMINAL
    assert requests == []


@pytest.mark.asyncio
async def test_downloader_revalidates_redirects_and_never_sends_credentials() -> None:
    requests: list[httpx.Request] = []
    resolved: list[tuple[str, int]] = []

    async def resolver(host: str, port: int) -> Sequence[str]:
        resolved.append((host, port))
        return ["93.184.216.34"]

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "public.example":
            return httpx.Response(
                302,
                headers={"Location": "https://cdn.example/final.png"},
            )
        return httpx.Response(
            200,
            content=b"fictional-image-bytes",
            headers={"Content-Type": "image/png"},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond),
        headers={
            "Authorization": "Bearer must-never-leak",
            "Cookie": "session=must-never-leak",
        },
    ) as client:
        downloader = SafeResultDownloader(
            client,
            resolver=resolver,
            max_bytes=1024,
        )
        content = await downloader.download(
            "https://public.example/start.png?signature=fictional",
            expected_mime_type="image/png",
        )

    assert content == b"fictional-image-bytes"
    assert resolved == [("public.example", 443), ("cdn.example", 443)]
    assert len(requests) == 2
    assert all("authorization" not in request.headers for request in requests)
    assert all("cookie" not in request.headers for request in requests)


@pytest.mark.asyncio
async def test_downloader_rejects_private_redirect_before_second_request() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={"Location": "https://127.0.0.1/out"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond)
    ) as client:
        downloader = SafeResultDownloader(
            client,
            resolver=_public_resolver,
            max_bytes=1024,
        )
        with pytest.raises(AgentError) as caught:
            await downloader.download(
                "https://public.example/start",
                expected_mime_type="image/png",
            )

    assert caught.value.detail.category == ErrorCategory.PROVIDER_TERMINAL
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_downloader_streams_with_a_hard_size_limit() -> None:
    def oversized(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"x" * 9,
            headers={"Content-Type": "image/png"},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(oversized)
    ) as client:
        downloader = SafeResultDownloader(
            client,
            resolver=_public_resolver,
            max_bytes=8,
        )
        with pytest.raises(AgentError) as caught:
            await downloader.download(
                "https://public.example/large.png",
                expected_mime_type="image/png",
            )

    assert caught.value.detail.category == ErrorCategory.PROVIDER_TERMINAL
    assert "too_large" in caught.value.detail.technical_detail
