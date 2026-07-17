from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager

import httpx
import pytest

from feishu_generation_agent.domain.errors import AgentError, ErrorCategory
from feishu_generation_agent.integrations.safe_download import SafeResultDownloader


async def _public_resolver(host: str, port: int) -> Sequence[str]:
    del host, port
    return ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"]


@asynccontextmanager
async def _downloader(
    respond: Callable[[httpx.Request], httpx.Response],
    *,
    resolver: Callable[[str, int], Awaitable[Sequence[str]]],
    max_bytes: int = 1024,
) -> AsyncIterator[SafeResultDownloader]:
    downloader = SafeResultDownloader(
        transport=httpx.MockTransport(respond),
        resolver=resolver,
        max_bytes=max_bytes,
    )
    try:
        yield downloader
    finally:
        await downloader.aclose()


@pytest.mark.asyncio
async def test_downloader_pins_validated_ip_and_preserves_host_and_sni() -> None:
    requests: list[httpx.Request] = []
    resolver_calls = 0

    async def rebinding_resolver(host: str, port: int) -> Sequence[str]:
        nonlocal resolver_calls
        resolver_calls += 1
        assert (host, port) == ("public.example", 443)
        if resolver_calls > 1:
            return ["127.0.0.1"]
        return ["93.184.216.34"]

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            content=b"fictional-pinned-image",
            headers={"Content-Type": "image/png"},
        )

    async with _downloader(respond, resolver=rebinding_resolver) as downloader:
        content = await downloader.download(
            "https://public.example/result.png?signature=fake",
            expected_mime_type="image/png",
        )

    assert content == b"fictional-pinned-image"
    assert resolver_calls == 1
    assert len(requests) == 1
    assert requests[0].url.host == "93.184.216.34"
    assert requests[0].headers["host"] == "public.example"
    assert requests[0].extensions["sni_hostname"] == "public.example"


@pytest.mark.asyncio
async def test_downloader_brackets_pinned_ipv6_url() -> None:
    requests: list[httpx.Request] = []

    async def ipv6_resolver(host: str, port: int) -> Sequence[str]:
        assert (host, port) == ("ipv6.example", 443)
        return ["2606:2800:220:1:248:1893:25c8:1946"]

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            content=b"fictional-ipv6-image",
            headers={"Content-Type": "image/png"},
        )

    async with _downloader(respond, resolver=ipv6_resolver) as downloader:
        await downloader.download(
            "https://ipv6.example/result.png",
            expected_mime_type="image/png",
        )

    assert str(requests[0].url).startswith(
        "https://[2606:2800:220:1:248:1893:25c8:1946]/"
    )
    assert requests[0].headers["host"] == "ipv6.example"
    assert requests[0].extensions["sni_hostname"] == "ipv6.example"


@pytest.mark.asyncio
async def test_downloader_brackets_original_ipv6_host_header() -> None:
    requests: list[httpx.Request] = []
    expanded = "2606:2800:0220:0001:0248:1893:25c8:1946"

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            content=b"fictional-literal-ipv6-image",
            headers={"Content-Type": "image/png"},
        )

    async with _downloader(respond, resolver=_public_resolver) as downloader:
        await downloader.download(
            f"https://[{expanded}]/result.png",
            expected_mime_type="image/png",
        )

    assert requests[0].headers["host"] == f"[{expanded}]"
    assert requests[0].extensions["sni_hostname"] == expanded


@pytest.mark.asyncio
async def test_downloader_owns_client_with_environment_proxies_disabled() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            content=b"fictional-no-proxy-image",
            headers={"Content-Type": "image/png"},
        )

    downloader = SafeResultDownloader(
        transport=httpx.MockTransport(respond),
        resolver=_public_resolver,
        max_bytes=1024,
    )
    try:
        await downloader.download(
            "https://public.example/result.png",
            expected_mime_type="image/png",
        )
        assert downloader._http_client.trust_env is False
    finally:
        await downloader.aclose()

    assert len(requests) == 1


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

    async with _downloader(unexpected, resolver=_public_resolver) as downloader:
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

    async with _downloader(unexpected, resolver=mixed_resolver) as downloader:
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
        if request.headers["host"] == "public.example":
            return httpx.Response(
                302,
                headers={"Location": "https://cdn.example/final.png"},
            )
        return httpx.Response(
            200,
            content=b"fictional-image-bytes",
            headers={"Content-Type": "image/png"},
        )

    async with _downloader(respond, resolver=resolver) as downloader:
        downloader._http_client.headers["Authorization"] = (
            "Bearer must-never-leak"
        )
        downloader._http_client.headers["Cookie"] = "session=must-never-leak"
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

    async with _downloader(respond, resolver=_public_resolver) as downloader:
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

    async with _downloader(
        oversized,
        resolver=_public_resolver,
        max_bytes=8,
    ) as downloader:
        with pytest.raises(AgentError) as caught:
            await downloader.download(
                "https://public.example/large.png",
                expected_mime_type="image/png",
            )

    assert caught.value.detail.category == ErrorCategory.PROVIDER_TERMINAL
    assert "too_large" in caught.value.detail.technical_detail
