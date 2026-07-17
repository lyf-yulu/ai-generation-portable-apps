import asyncio
from collections.abc import Mapping
from time import monotonic
from typing import Any

import httpx

from feishu_generation_agent.config import Settings
from feishu_generation_agent.domain.errors import (
    AgentError,
    ErrorCategory,
    ErrorDetail,
)


_BASE_URL = "https://open.feishu.cn"
_TOKEN_PATH = "/open-apis/auth/v3/tenant_access_token/internal"
_TOKEN_INVALID_CODE = 99991663
_PERMISSION_CODES = frozenset(
    {
        _TOKEN_INVALID_CODE,
        99991664,
        99991668,
        99991670,
        99991672,
        1770032,
        1770033,
        1770034,
    }
)


class FeishuClient:
    def __init__(
        self,
        settings: Settings,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._app_id = settings.lark_app_id
        self._app_secret = (
            settings.lark_app_secret.get_secret_value()
            if settings.lark_app_secret is not None
            else None
        )
        self._http_client = http_client or httpx.AsyncClient(
            base_url=_BASE_URL,
            timeout=30,
        )
        self._owns_http_client = http_client is None
        self._tenant_access_token: str | None = None
        self._token_valid_until = 0.0
        self._token_lock = asyncio.Lock()

    async def close(self) -> None:
        if self._owns_http_client:
            await self._http_client.aclose()

    async def tenant_token(self) -> str:
        if self._token_is_valid():
            return self._tenant_access_token or ""
        async with self._token_lock:
            if self._token_is_valid():
                return self._tenant_access_token or ""
            return await self._fetch_tenant_token()

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
    ) -> dict:
        token = await self.tenant_token()
        for attempt in range(2):
            response = await self._send(
                method,
                path,
                token,
                params=params,
                json_body=json_body,
            )
            payload = self._optional_response_json(response)
            if self._authentication_failed(response, payload) and attempt == 0:
                token = await self._refresh_after_auth_failure(token)
                continue
            if not payload and response.status_code < 400:
                raise self._document_error(
                    "飞书接口返回了无法解析的响应",
                    f"{method} {path}: HTTP {response.status_code}, invalid JSON",
                )
            self._raise_for_api_error(response, payload, method, path)
            return payload
        raise AssertionError("request retry loop exhausted")

    async def iter_items(
        self,
        path: str,
        *,
        params: dict | None = None,
    ) -> list[dict]:
        page_params = dict(params or {})
        page_params["page_size"] = 500
        items: list[dict] = []
        seen_tokens: set[str] = set()

        while True:
            payload = await self.request_json("GET", path, params=page_params)
            data = payload.get("data")
            if not isinstance(data, Mapping):
                raise self._document_error(
                    "飞书文档分页响应缺少 data",
                    f"GET {path}: data is not an object",
                )
            page_items = data.get("items", [])
            if not isinstance(page_items, list) or not all(
                isinstance(item, dict) for item in page_items
            ):
                raise self._document_error(
                    "飞书文档分页响应中的 items 无效",
                    f"GET {path}: items is not a list of objects",
                )
            items.extend(page_items)
            if not data.get("has_more", False):
                return items

            next_token = data.get("page_token")
            if not isinstance(next_token, str) or not next_token:
                raise self._document_error(
                    "飞书文档分页响应缺少下一页标记",
                    f"GET {path}: has_more without page_token",
                )
            if next_token in seen_tokens:
                raise self._document_error(
                    "飞书文档分页标记重复，已停止读取",
                    f"GET {path}: repeated page_token",
                )
            seen_tokens.add(next_token)
            page_params["page_token"] = next_token

    async def download_media(self, file_token: str) -> tuple[bytes, str]:
        path = f"/open-apis/drive/v1/medias/{file_token}/download"
        token = await self.tenant_token()
        for attempt in range(2):
            response = await self._send("GET", path, token)
            payload = self._optional_response_json(response)
            if self._authentication_failed(response, payload) and attempt == 0:
                token = await self._refresh_after_auth_failure(token)
                continue
            if response.status_code >= 400 or self._api_code(payload) != 0:
                self._raise_for_api_error(response, payload, "GET", path)
            return (
                response.content,
                response.headers.get("Content-Type", "application/octet-stream"),
            )
        raise AssertionError("download retry loop exhausted")

    def _token_is_valid(self) -> bool:
        return (
            self._tenant_access_token is not None
            and monotonic() < self._token_valid_until
        )

    async def _fetch_tenant_token(self) -> str:
        if not self._app_id or not self._app_secret:
            raise AgentError(
                ErrorDetail(
                    category=ErrorCategory.CONFIGURATION,
                    message="飞书应用凭证未配置",
                    technical_detail="LARK_APP_ID or LARK_APP_SECRET is missing",
                    retryable=False,
                )
            )
        try:
            response = await self._http_client.post(
                _TOKEN_PATH,
                json={"app_id": self._app_id, "app_secret": self._app_secret},
            )
        except httpx.HTTPError as exc:
            raise self._transport_error("POST", _TOKEN_PATH, exc) from exc

        payload = self._optional_response_json(response)
        code = self._api_code(payload)
        if response.status_code >= 400 or code != 0:
            category = (
                ErrorCategory.TRANSIENT
                if response.status_code == 429 or response.status_code >= 500
                else ErrorCategory.CONFIGURATION
            )
            raise AgentError(
                ErrorDetail(
                    category=category,
                    message="无法获取飞书 tenant access token",
                    technical_detail=self._technical_detail(
                        "POST", _TOKEN_PATH, response.status_code, payload
                    ),
                    retryable=category == ErrorCategory.TRANSIENT,
                )
            )

        if not payload:
            raise AgentError(
                ErrorDetail(
                    category=ErrorCategory.CONFIGURATION,
                    message="飞书 tenant access token 响应无效",
                    technical_detail="token response is not valid JSON",
                    retryable=False,
                )
            )

        token = payload.get("tenant_access_token")
        expires_in = payload.get("expire", 0)
        if not isinstance(token, str) or not token or not isinstance(
            expires_in, (int, float)
        ):
            raise AgentError(
                ErrorDetail(
                    category=ErrorCategory.CONFIGURATION,
                    message="飞书 tenant access token 响应无效",
                    technical_detail="token response missing token or expire",
                    retryable=False,
                )
            )
        self._tenant_access_token = token
        self._token_valid_until = monotonic() + max(float(expires_in) - 60, 0)
        return token

    async def _refresh_after_auth_failure(self, failed_token: str) -> str:
        async with self._token_lock:
            if (
                self._tenant_access_token is not None
                and self._tenant_access_token != failed_token
                and self._token_is_valid()
            ):
                return self._tenant_access_token
            self._tenant_access_token = None
            self._token_valid_until = 0.0
            return await self._fetch_tenant_token()

    async def _send(
        self,
        method: str,
        path: str,
        token: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
    ) -> httpx.Response:
        try:
            return await self._http_client.request(
                method,
                path,
                params=params,
                json=json_body,
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as exc:
            raise self._transport_error(method, path, exc) from exc

    def _raise_for_api_error(
        self,
        response: httpx.Response,
        payload: dict,
        method: str,
        path: str,
    ) -> None:
        code = self._api_code(payload)
        if response.status_code < 400 and code == 0:
            return

        if response.status_code in {401, 403} or code in _PERMISSION_CODES:
            category = ErrorCategory.PERMISSION
            message = "没有权限读取该飞书文档或素材"
            retryable = False
        elif response.status_code == 429 or response.status_code >= 500:
            category = ErrorCategory.TRANSIENT
            message = "飞书服务暂时不可用，请稍后重试"
            retryable = True
        else:
            category = ErrorCategory.DOCUMENT
            message = "飞书文档响应无效或不受支持"
            retryable = False
        raise AgentError(
            ErrorDetail(
                category=category,
                message=message,
                technical_detail=self._technical_detail(
                    method, path, response.status_code, payload
                ),
                retryable=retryable,
            )
        )

    @staticmethod
    def _response_json(
        response: httpx.Response,
        method: str,
        path: str,
    ) -> dict:
        payload = FeishuClient._optional_response_json(response)
        if not payload:
            raise FeishuClient._document_error(
                "飞书接口返回了无法解析的响应",
                f"{method} {path}: HTTP {response.status_code}, invalid JSON",
            )
        return payload

    @staticmethod
    def _optional_response_json(response: httpx.Response) -> dict:
        try:
            payload = response.json()
        except (ValueError, UnicodeDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _api_code(payload: dict) -> int:
        code = payload.get("code", 0)
        return code if isinstance(code, int) else -1

    @staticmethod
    def _authentication_failed(response: httpx.Response, payload: dict) -> bool:
        return (
            response.status_code == 401
            or FeishuClient._api_code(payload) == _TOKEN_INVALID_CODE
        )

    @staticmethod
    def _technical_detail(
        method: str,
        path: str,
        status: int,
        payload: dict,
    ) -> str:
        code = payload.get("code")
        message = payload.get("msg") or payload.get("message") or ""
        return f"{method} {path}: HTTP {status}, code={code}, msg={message}"

    @staticmethod
    def _document_error(message: str, detail: str) -> AgentError:
        return AgentError(
            ErrorDetail(
                category=ErrorCategory.DOCUMENT,
                message=message,
                technical_detail=detail,
                retryable=False,
            )
        )

    @staticmethod
    def _transport_error(method: str, path: str, exc: Exception) -> AgentError:
        return AgentError(
            ErrorDetail(
                category=ErrorCategory.TRANSIENT,
                message="连接飞书服务失败，请稍后重试",
                technical_detail=f"{method} {path}: {type(exc).__name__}",
                retryable=True,
            )
        )
