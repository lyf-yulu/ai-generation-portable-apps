from typing import Protocol
from urllib.parse import urlsplit

import httpx


class PublicMediaHost(Protocol):
    async def upload(
        self,
        content: bytes,
        filename: str,
        mime_type: str,
    ) -> str: ...


class PublicMediaUploadError(RuntimeError):
    """A reference video or audio file could not be hosted publicly."""


class UguuPublicMediaHost:
    _ENDPOINT = "https://uguu.se/upload.php"

    def __init__(self, http_client: httpx.AsyncClient) -> None:
        self._http_client = http_client

    async def upload(
        self,
        content: bytes,
        filename: str,
        mime_type: str,
    ) -> str:
        if not content:
            raise PublicMediaUploadError("临时托管失败：参考素材为空")
        try:
            response = await self._http_client.post(
                self._ENDPOINT,
                files={"files[]": (filename, content, mime_type)},
                timeout=httpx.Timeout(60, connect=10),
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError, TypeError):
            raise PublicMediaUploadError("临时托管失败，请稍后重试") from None

        try:
            url = payload["files"][0]["url"]
        except (KeyError, IndexError, TypeError):
            raise PublicMediaUploadError("临时托管失败：服务未返回素材地址") from None
        if not isinstance(url, str):
            raise PublicMediaUploadError("临时托管失败：服务返回的素材地址无效")
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise PublicMediaUploadError("临时托管失败：素材地址必须是匿名 HTTPS 地址")
        return url
