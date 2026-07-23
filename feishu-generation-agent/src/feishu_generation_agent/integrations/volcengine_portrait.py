import asyncio
import hashlib
import hmac
import json
from datetime import UTC, datetime
from io import BytesIO
from math import ceil
from typing import Any
from urllib.parse import urlencode

import httpx
from PIL import Image, UnidentifiedImageError
from pydantic import SecretStr

from feishu_generation_agent.domain.document import MediaAsset
from feishu_generation_agent.domain.errors import AgentError, ErrorCategory, ErrorDetail
from feishu_generation_agent.integrations.public_media import (
    PublicMediaHost,
    PublicMediaUploadError,
)
from feishu_generation_agent.storage.portrait_assets import PortraitAssetStore
from feishu_generation_agent.integrations.seedance import SeedanceVideoGenerator


_OPENAPI_URL = "https://ark.cn-beijing.volcengineapi.com/"
_OPENAPI_HOST = "ark.cn-beijing.volcengineapi.com"
_REGION = "cn-beijing"
_SERVICE = "ark"
_MIN_ASSET_DIMENSION = 300
_RESIZABLE_IMAGE_FORMATS = frozenset({"JPEG", "PNG", "WEBP"})


def _secret(value: str | SecretStr) -> str:
    return value.get_secret_value() if isinstance(value, SecretStr) else value


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sign(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def _portrait_upload_content(asset: MediaAsset) -> bytes:
    content = asset.local_path.read_bytes()
    try:
        with Image.open(BytesIO(content)) as image:
            width, height = image.size
            if width >= _MIN_ASSET_DIMENSION and height >= _MIN_ASSET_DIMENSION:
                return content
            if image.format not in _RESIZABLE_IMAGE_FORMATS:
                raise VolcengineAssetClient._validation_error(
                    "真人参考图格式不支持自动放大"
                )
            scale = max(
                _MIN_ASSET_DIMENSION / width,
                _MIN_ASSET_DIMENSION / height,
            )
            target_size = (ceil(width * scale), ceil(height * scale))
            resized = image.resize(target_size, Image.Resampling.LANCZOS)
            output = BytesIO()
            resized.save(output, format=image.format)
            return output.getvalue()
    except (OSError, UnidentifiedImageError) as exc:
        raise VolcengineAssetClient._validation_error(
            "真人参考图无法读取"
        ) from exc


class VolcengineAssetClient:
    def __init__(
        self,
        http_client: httpx.AsyncClient,
        *,
        access_key: str | SecretStr,
        secret_key: str | SecretStr,
        project_name: str,
        public_media_host: PublicMediaHost,
        store: PortraitAssetStore,
        poll_interval_seconds: float = 1.0,
        max_poll_attempts: int = 300,
    ) -> None:
        access_key = _secret(access_key).strip()
        secret_key = _secret(secret_key).strip()
        if not access_key or not secret_key or not project_name.strip():
            raise ValueError("真人资产配置不完整")
        self._http = http_client
        self._access_key = SecretStr(access_key)
        self._secret_key = SecretStr(secret_key)
        self._project_name = project_name.strip()
        self._public_media_host = public_media_host
        self._store = store
        self._poll_interval_seconds = poll_interval_seconds
        self._max_poll_attempts = max_poll_attempts

    async def ensure_image_asset(self, run_id: str, asset: MediaAsset) -> str:
        if not run_id or not asset.mime_type.startswith("image/"):
            raise self._validation_error("真人视频只支持图片参考素材")
        existing = await self._store.get_asset(run_id, asset.asset_id)
        if existing is not None:
            asset_id, status = existing
            if status != "Active":
                await self._wait_for_active(run_id, asset.asset_id, asset_id)
            return f"asset://{asset_id}"

        group_id = await self._ensure_group(run_id)
        try:
            source_url = await self._public_media_host.upload(
                _portrait_upload_content(asset),
                asset.local_path.name,
                asset.mime_type,
            )
        except (OSError, PublicMediaUploadError) as exc:
            raise self._transient_error("真人素材临时托管失败") from exc
        result = await self._call(
            "CreateAsset",
            {
                "GroupId": group_id,
                "URL": source_url,
                "AssetType": "Image",
                "ProjectName": self._project_name,
                "Name": asset.local_path.name,
            },
        )
        asset_id = self._result_id(result, "CreateAsset")
        await self._store.save_asset(run_id, asset.asset_id, asset_id, "Processing")
        await self._wait_for_active(run_id, asset.asset_id, asset_id)
        return f"asset://{asset_id}"

    async def _ensure_group(self, run_id: str) -> str:
        existing = await self._store.get_group_id(run_id)
        if existing:
            return existing
        result = await self._call(
            "CreateAssetGroup",
            {
                "Name": f"真人类-{run_id}",
                "GroupType": "AIGC",
                "ProjectName": self._project_name,
            },
        )
        group_id = self._result_id(result, "CreateAssetGroup")
        await self._store.save_group_id(run_id, group_id)
        return group_id

    async def _wait_for_active(
        self, run_id: str, source_asset_id: str, asset_id: str
    ) -> None:
        for _ in range(self._max_poll_attempts):
            result = await self._call(
                "GetAsset", {"Id": asset_id, "ProjectName": self._project_name}
            )
            status = self._result_status(result)
            await self._store.save_asset(run_id, source_asset_id, asset_id, status)
            if status == "Active":
                return
            if status == "Failed":
                raise self._terminal_error("真人素材处理失败")
            await asyncio.sleep(self._poll_interval_seconds)
        raise self._transient_error("真人素材处理超时")

    async def _call(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        now = datetime.now(UTC)
        headers = self._signed_headers(action, payload, now)
        try:
            response = await self._http.post(
                _OPENAPI_URL,
                params={"Action": action, "Version": "2024-01-01"},
                content=payload.encode("utf-8"),
                headers=headers,
                timeout=httpx.Timeout(120, connect=10),
            )
        except httpx.HTTPError as exc:
            raise self._transient_error("火山真人资产服务暂时不可用") from exc
        try:
            data = response.json()
        except ValueError as exc:
            if 400 <= response.status_code < 500:
                raise self._rejection_error(
                    action, response.status_code, {}
                ) from exc
            raise self._transient_error("火山真人资产服务暂时不可用") from exc
        if 400 <= response.status_code < 500:
            raise self._rejection_error(action, response.status_code, data)
        if response.status_code >= 500:
            raise self._transient_error("火山真人资产服务暂时不可用")
        try:
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise self._transient_error("火山真人资产服务暂时不可用") from exc
        if not isinstance(data, dict):
            raise self._terminal_error("火山真人资产响应无效")
        return data

    @staticmethod
    def _rejection_error(
        action: str, status_code: int, payload: object
    ) -> AgentError:
        metadata = payload.get("ResponseMetadata") if isinstance(payload, dict) else None
        error = metadata.get("Error") if isinstance(metadata, dict) else None
        code = error.get("Code") if isinstance(error, dict) else None
        message = error.get("Message") if isinstance(error, dict) else None
        parts = [f"{action} HTTP {status_code}"]
        if isinstance(code, str) and code:
            parts.append(code)
        if isinstance(message, str) and message:
            parts.append(message)
        technical_detail = ": ".join(parts)
        user_reason = (
            message
            if isinstance(message, str) and message
            else "请求参数不符合要求"
        )
        return AgentError(
            ErrorDetail(
                category=ErrorCategory.PROVIDER_TERMINAL,
                message=f"火山真人资产请求被拒绝：{user_reason}",
                technical_detail=technical_detail,
                retryable=False,
            )
        )

    def _signed_headers(self, action: str, payload: str, now: datetime) -> dict[str, str]:
        date_time = now.strftime("%Y%m%dT%H%M%SZ")
        date = now.strftime("%Y%m%d")
        query = urlencode(sorted({"Action": action, "Version": "2024-01-01"}.items()))
        headers = {
            "Content-Type": "application/json",
            "Host": _OPENAPI_HOST,
            "X-Content-Sha256": _hash(payload),
            "X-Date": date_time,
        }
        signed_names = ";".join(key.lower() for key in sorted(headers, key=str.lower))
        canonical_headers = "".join(
            f"{key.lower()}:{headers[key]}\n" for key in sorted(headers, key=str.lower)
        )
        canonical = f"POST\n/\n{query}\n{canonical_headers}\n{signed_names}\n{_hash(payload)}"
        scope = f"{date}/{_REGION}/{_SERVICE}/request"
        string_to_sign = f"HMAC-SHA256\n{date_time}\n{scope}\n{_hash(canonical)}"
        secret = self._secret_key.get_secret_value().encode("utf-8")
        signing_key = _sign(_sign(_sign(_sign(secret, date), _REGION), _SERVICE), "request")
        signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
        headers["Authorization"] = (
            "HMAC-SHA256 Credential="
            f"{self._access_key.get_secret_value()}/{scope}, "
            f"SignedHeaders={signed_names}, Signature={signature}"
        )
        return headers

    @staticmethod
    def _result_id(payload: dict[str, Any], action: str) -> str:
        result = payload.get("Result", payload)
        asset_id = result.get("Id") if isinstance(result, dict) else None
        if not isinstance(asset_id, str) or not asset_id:
            raise VolcengineAssetClient._terminal_error(f"{action} 未返回资产标识")
        return asset_id

    @staticmethod
    def _result_status(payload: dict[str, Any]) -> str:
        result = payload.get("Result", payload)
        status = result.get("Status") if isinstance(result, dict) else None
        if not isinstance(status, str) or not status:
            raise VolcengineAssetClient._terminal_error("GetAsset 未返回状态")
        return status

    @staticmethod
    def _validation_error(message: str) -> AgentError:
        return AgentError(
            ErrorDetail(
                category=ErrorCategory.VALIDATION,
                message=message,
                technical_detail=message,
                retryable=False,
            )
        )

    @staticmethod
    def _terminal_error(message: str) -> AgentError:
        return AgentError(
            ErrorDetail(
                category=ErrorCategory.PROVIDER_TERMINAL,
                message=message,
                technical_detail=message,
                retryable=False,
            )
        )

    @staticmethod
    def _transient_error(message: str) -> AgentError:
        return AgentError(
            ErrorDetail(
                category=ErrorCategory.TRANSIENT,
                message=message,
                technical_detail=message,
                retryable=True,
            )
        )


class VolcenginePortraitVideoGenerator:
    def __init__(
        self,
        http_client: httpx.AsyncClient,
        *,
        asset_client: VolcengineAssetClient,
        base_url: str,
        api_key: str | SecretStr,
        model: str,
        public_media_host: PublicMediaHost,
    ) -> None:
        self._http = http_client
        self._asset_client = asset_client
        self._base_url = base_url
        self._api_key = api_key
        self._model = model
        self._public_media_host = public_media_host

    def for_run(self, run_id: str) -> SeedanceVideoGenerator:
        async def resolve_image(task: Any, reference: Any, asset: MediaAsset, content: bytes) -> str:
            del task, reference, content
            return await self._asset_client.ensure_image_asset(run_id, asset)

        return SeedanceVideoGenerator(
            self._http,
            base_url=self._base_url,
            api_key=self._api_key,
            model=self._model,
            public_media_host=self._public_media_host,
            provider_name="volcengine_portrait",
            image_url_resolver=resolve_image,
        )
