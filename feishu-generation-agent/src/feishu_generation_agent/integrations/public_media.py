from datetime import UTC, datetime
import hashlib
import hmac
import mimetypes
from pathlib import Path
from typing import Protocol
from urllib.parse import quote, urlsplit
from uuid import uuid4

import httpx


class PublicMediaHost(Protocol):
    async def upload(
        self,
        content: bytes,
        filename: str,
        mime_type: str,
    ) -> str: ...


class PublicMediaUploadError(RuntimeError):
    """A reference media file could not be hosted publicly."""


class TosPublicMediaHost:
    def __init__(
        self,
        http_client: httpx.AsyncClient,
        *,
        access_key: str,
        secret_key: str,
        bucket: str,
        region: str = "cn-beijing",
    ) -> None:
        self._http_client = http_client
        self._access_key = access_key.strip()
        self._secret_key = secret_key.strip()
        self._bucket = bucket.strip()
        self._region = region.strip()
        if not all(
            (self._access_key, self._secret_key, self._bucket, self._region)
        ):
            raise ValueError("TOS reference media configuration is incomplete")

    @staticmethod
    def _sha256(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def _hmac(key: bytes, message: str) -> bytes:
        return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()

    def _signing_key(self, date_stamp: str) -> bytes:
        date_key = self._hmac(self._secret_key.encode("utf-8"), date_stamp)
        region_key = self._hmac(date_key, self._region)
        service_key = self._hmac(region_key, "tos")
        return self._hmac(service_key, "request")

    def _put_headers(
        self,
        object_key: str,
        content: bytes,
        mime_type: str,
        now: datetime,
    ) -> dict[str, str]:
        host = f"{self._bucket}.tos-{self._region}.volces.com"
        request_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = request_date[:8]
        payload_hash = self._sha256(content)
        headers = {
            "Host": host,
            "Content-Type": mime_type,
            "x-tos-content-sha256": payload_hash,
            "x-tos-date": request_date,
        }
        signed_names = sorted(headers, key=str.lower)
        canonical_headers = "".join(
            f"{name.lower()}:{headers[name].strip()}\n"
            for name in signed_names
        )
        signed_headers = ";".join(name.lower() for name in signed_names)
        canonical_uri = "/" + quote(object_key, safe="/")
        canonical_request = (
            f"PUT\n{canonical_uri}\n\n{canonical_headers}\n"
            f"{signed_headers}\n{payload_hash}"
        )
        scope = f"{date_stamp}/{self._region}/tos/request"
        string_to_sign = (
            f"TOS4-HMAC-SHA256\n{request_date}\n{scope}\n"
            f"{self._sha256(canonical_request.encode('utf-8'))}"
        )
        signature = hmac.new(
            self._signing_key(date_stamp),
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        headers["Authorization"] = (
            f"TOS4-HMAC-SHA256 Credential={self._access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        return headers

    def _presigned_get_url(
        self,
        object_key: str,
        now: datetime,
        *,
        expires: int = 43_200,
    ) -> str:
        host = f"{self._bucket}.tos-{self._region}.volces.com"
        request_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = request_date[:8]
        scope = f"{date_stamp}/{self._region}/tos/request"
        query = {
            "X-Tos-Algorithm": "TOS4-HMAC-SHA256",
            "X-Tos-Credential": f"{self._access_key}/{scope}",
            "X-Tos-Date": request_date,
            "X-Tos-Expires": str(expires),
            "X-Tos-SignedHeaders": "host",
        }
        canonical_query = "&".join(
            f"{quote(key, safe='')}={quote(query[key], safe='')}"
            for key in sorted(query)
        )
        canonical_uri = "/" + quote(object_key, safe="/")
        canonical_request = (
            f"GET\n{canonical_uri}\n{canonical_query}\n"
            f"host:{host}\n\nhost\nUNSIGNED-PAYLOAD"
        )
        string_to_sign = (
            f"TOS4-HMAC-SHA256\n{request_date}\n{scope}\n"
            f"{self._sha256(canonical_request.encode('utf-8'))}"
        )
        signature = hmac.new(
            self._signing_key(date_stamp),
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return (
            f"https://{host}{canonical_uri}?{canonical_query}"
            f"&X-Tos-Signature={signature}"
        )

    async def upload(
        self,
        content: bytes,
        filename: str,
        mime_type: str,
    ) -> str:
        if not content:
            raise PublicMediaUploadError("TOS 临时托管失败：参考素材为空")
        suffix = Path(filename).suffix or mimetypes.guess_extension(mime_type)
        object_key = f"refmedia/{uuid4().hex}{suffix or '.bin'}"
        host = f"{self._bucket}.tos-{self._region}.volces.com"
        now = datetime.now(UTC)
        try:
            response = await self._http_client.put(
                f"https://{host}/{quote(object_key, safe='/')}",
                content=content,
                headers=self._put_headers(object_key, content, mime_type, now),
                timeout=httpx.Timeout(300, connect=10),
                follow_redirects=False,
            )
            response.raise_for_status()
        except httpx.HTTPError:
            raise PublicMediaUploadError("TOS 临时托管失败，请稍后重试") from None
        return self._presigned_get_url(object_key, now)


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
