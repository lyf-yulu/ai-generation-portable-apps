import asyncio
from collections.abc import Awaitable, Callable, Sequence
import ipaddress
import socket
from typing import Protocol, runtime_checkable
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from feishu_generation_agent.domain.errors import (
    AgentError,
    ErrorCategory,
    ErrorDetail,
)


HostResolver = Callable[[str, int], Awaitable[Sequence[str]]]
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_BENCHMARK_NETWORK = ipaddress.ip_network("198.18.0.0/15")


@runtime_checkable
class ResultDownloader(Protocol):
    async def download(
        self,
        url: str,
        *,
        expected_mime_type: str,
    ) -> bytes:
        raise NotImplementedError


class SafeResultDownloader:
    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: HostResolver | None = None,
        max_bytes: int,
        max_redirects: int = 3,
        allow_benchmark_dns: bool = False,
    ) -> None:
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if (
            not isinstance(max_redirects, int)
            or isinstance(max_redirects, bool)
            or max_redirects < 0
        ):
            raise ValueError("max_redirects must be non-negative")
        self._http_client = httpx.AsyncClient(
            transport=transport,
            trust_env=False,
            follow_redirects=False,
        )
        self._resolver = resolver or self._resolve_host
        self._max_bytes = max_bytes
        self._max_redirects = max_redirects
        self._allow_benchmark_dns = allow_benchmark_dns

    async def aclose(self) -> None:
        await self._http_client.aclose()

    async def download(self, url: str, *, expected_mime_type: str) -> bytes:
        current_url = url
        for redirect_count in range(self._max_redirects + 1):
            hostname, address = await self._validated_target(current_url)
            parsed = urlsplit(current_url)
            pinned_host = (
                f"[{address.compressed}]"
                if isinstance(address, ipaddress.IPv6Address)
                else address.compressed
            )
            pinned_url = urlunsplit(
                ("https", pinned_host, parsed.path, parsed.query, "")
            )
            host_header = (
                f"[{hostname}]"
                if ":" in hostname
                else hostname
            )
            request = self._http_client.build_request(
                "GET",
                pinned_url,
                headers={
                    "Accept": expected_mime_type,
                    "Host": host_header,
                },
            )
            request.extensions["sni_hostname"] = hostname
            for header_name in (
                "authorization",
                "cookie",
                "proxy-authorization",
            ):
                if header_name in request.headers:
                    del request.headers[header_name]
            try:
                response = await self._http_client.send(
                    request,
                    stream=True,
                    auth=None,
                    follow_redirects=False,
                )
            except httpx.TransportError as exc:
                raise self._error(
                    ErrorCategory.TRANSIENT,
                    "下载 Chiyun 图片失败，请稍后重试",
                    f"operation=result_download; cause={type(exc).__name__}",
                    retryable=True,
                ) from None
            try:
                if response.status_code in _REDIRECT_STATUSES:
                    location = response.headers.get("location")
                    if not location or redirect_count >= self._max_redirects:
                        raise self._provider_error("redirect_limit")
                    current_url = urljoin(current_url, location)
                    continue
                if not 200 <= response.status_code < 300:
                    if response.status_code == 429 or response.status_code >= 500:
                        raise self._error(
                            ErrorCategory.TRANSIENT,
                            "下载 Chiyun 图片失败，请稍后重试",
                            (
                                "operation=result_download; "
                                f"status={response.status_code}"
                            ),
                            retryable=True,
                        )
                    raise self._provider_error("http_status")
                self._validate_content_type(response, expected_mime_type)
                declared_size = response.headers.get("content-length")
                if declared_size is not None:
                    try:
                        if int(declared_size) > self._max_bytes:
                            raise self._provider_error("result_too_large")
                    except ValueError:
                        pass
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(content) + len(chunk) > self._max_bytes:
                        raise self._provider_error("result_too_large")
                    content.extend(chunk)
                if not content:
                    raise self._provider_error("empty_result")
                return bytes(content)
            finally:
                await response.aclose()
        raise self._provider_error("redirect_limit")

    async def _validated_target(
        self,
        url: str,
    ) -> tuple[str, ipaddress.IPv4Address | ipaddress.IPv6Address]:
        if not isinstance(url, str):
            raise self._provider_error("unsafe_url")
        try:
            parsed = urlsplit(url)
            hostname = parsed.hostname
            port = parsed.port
        except (TypeError, ValueError):
            raise self._provider_error("unsafe_url") from None
        if (
            parsed.scheme != "https"
            or not hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or port not in {None, 443}
        ):
            raise self._provider_error("unsafe_url")
        normalized_host = hostname.rstrip(".").lower()
        if (
            not normalized_host
            or normalized_host == "localhost"
            or normalized_host.endswith(".localhost")
            or "%" in normalized_host
        ):
            raise self._provider_error("unsafe_url")

        try:
            literal = ipaddress.ip_address(normalized_host)
        except ValueError:
            literal = None
        if literal is not None:
            if not self._is_public_address(literal):
                raise self._provider_error("unsafe_address")
            return normalized_host, literal

        try:
            addresses = await self._resolver(normalized_host, 443)
        except AgentError:
            raise
        except Exception as exc:
            raise self._error(
                ErrorCategory.TRANSIENT,
                "解析 Chiyun 图片地址失败，请稍后重试",
                f"operation=result_dns; cause={type(exc).__name__}",
                retryable=True,
            ) from None
        if not addresses:
            raise self._provider_error("missing_dns_address")
        validated: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
        for value in addresses:
            try:
                address = ipaddress.ip_address(value)
            except ValueError:
                raise self._provider_error("invalid_dns_address") from None
            if not self._is_public_address(address) and not (
                self._allow_benchmark_dns and address in _BENCHMARK_NETWORK
            ):
                raise self._provider_error("unsafe_address")
            validated.append(address)
        return normalized_host, validated[0]

    @staticmethod
    async def _resolve_host(host: str, port: int) -> Sequence[str]:
        records = await asyncio.to_thread(
            socket.getaddrinfo,
            host,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
        addresses: list[str] = []
        for record in records:
            address = record[4][0]
            if address not in addresses:
                addresses.append(address)
        return addresses

    @staticmethod
    def _is_public_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
        return bool(
            address.is_global
            and not address.is_private
            and not address.is_loopback
            and not address.is_link_local
            and not address.is_multicast
            and not address.is_unspecified
            and not address.is_reserved
        )

    @classmethod
    def _provider_error(cls, cause: str) -> AgentError:
        return cls._error(
            ErrorCategory.PROVIDER_TERMINAL,
            "Chiyun 图片下载地址不安全或结果无效",
            f"operation=result_download; cause={cause}",
            retryable=False,
        )

    @staticmethod
    def _validate_content_type(
        response: httpx.Response,
        expected_mime_type: str,
    ) -> None:
        declared = response.headers.get("content-type", "")
        declared = declared.split(";", 1)[0].strip().lower()
        if declared != expected_mime_type:
            raise SafeResultDownloader._provider_error("mime_mismatch")

    @staticmethod
    def _error(
        category: ErrorCategory,
        message: str,
        technical_detail: str,
        *,
        retryable: bool,
    ) -> AgentError:
        return AgentError(
            ErrorDetail(
                category=category,
                message=message,
                technical_detail=technical_detail,
                retryable=retryable,
            )
        )
