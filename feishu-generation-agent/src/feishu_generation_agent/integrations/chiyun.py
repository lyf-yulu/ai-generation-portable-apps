import base64
import binascii
from hashlib import sha256
import json
import logging
from typing import Any, Literal
from urllib.parse import quote, urlsplit

import httpx
from pydantic import BaseModel, Field, SecretStr

from feishu_generation_agent.domain.artifact import (
    ProviderResult,
    ProviderSubmission,
)
from feishu_generation_agent.domain.document import MediaAsset
from feishu_generation_agent.domain.errors import (
    AgentError,
    ErrorCategory,
    ErrorDetail,
)
from feishu_generation_agent.domain.plan import GenerationTask


_IMAGE_MIME_TYPES = frozenset(
    {"image/gif", "image/jpeg", "image/png", "image/webp"}
)
_LOGGER = logging.getLogger(__name__)
_DEFAULT_MAX_RESPONSE_BYTES = 64 * 1024 * 1024
_DEFAULT_MAX_RESULT_BYTES = 32 * 1024 * 1024


class ModelProbeResult(BaseModel):
    status: Literal["available", "unsupported"]
    model_ids: list[str] = Field(default_factory=list)
    configured_model_available: bool | None = None
    message: str


class ChiyunImageGenerator:
    def __init__(
        self,
        http_client: httpx.AsyncClient,
        *,
        base_url: str,
        api_key: str | SecretStr,
        model: str,
        max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
        max_result_bytes: int = _DEFAULT_MAX_RESULT_BYTES,
    ) -> None:
        parsed = urlsplit(base_url.strip())
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise self._error(
                ErrorCategory.CONFIGURATION,
                "Chiyun 服务地址配置无效",
                "field=base_url; expected=https origin",
            )
        secret = (
            api_key.get_secret_value()
            if isinstance(api_key, SecretStr)
            else api_key
        )
        if not secret.strip():
            raise self._error(
                ErrorCategory.CONFIGURATION,
                "Chiyun API Key 未配置",
                "field=api_key; cause=empty",
            )
        if not model.strip():
            raise self._error(
                ErrorCategory.CONFIGURATION,
                "Chiyun 模型未配置",
                "field=model; cause=empty",
            )
        self._http_client = http_client
        self._base_url = f"https://{parsed.netloc}"
        self._api_key = SecretStr(secret.strip())
        self._model = model.strip()
        self._max_response_bytes = max_response_bytes
        self._max_result_bytes = max_result_bytes
        self._timeout = httpx.Timeout(120, connect=10)

    async def submit(
        self,
        task: GenerationTask,
        assets: list[MediaAsset],
    ) -> ProviderSubmission:
        asset_contents = self._validate_assets(task, assets)
        parts: list[dict[str, Any]] = [{"text": self._prompt(task)}]
        for asset, content in zip(assets, asset_contents, strict=True):
            parts.append(
                {
                    "inline_data": {
                        "mime_type": asset.mime_type,
                        "data": base64.b64encode(content).decode("ascii"),
                    }
                }
            )
        payload = {
            "contents": [{"parts": parts}],
            "generationConfig": {
                "imageConfig": {
                    "aspectRatio": task.aspect_ratio,
                    "imageSize": task.image_size,
                }
            },
        }
        model_path = quote(self._model, safe="")
        body = await self._request_json(
            "POST",
            f"{self._base_url}/v1beta/models/{model_path}:generateContent",
            json_body=payload,
            operation="generate",
        )
        if body is None:
            raise AssertionError("generate endpoint cannot be unsupported")
        results = self._result_items(body)
        if not results:
            raise self._provider_error(
                "Chiyun 未返回图片结果",
                "operation=generate; cause=missing_result",
            )
        _LOGGER.info(
            "Chiyun generation completed result_count=%d mime_types=%s",
            len(results),
            ",".join(result.mime_type for result in results),
        )
        response_id = body.get("responseId")
        provider_task_id = (
            response_id
            if isinstance(response_id, str) and response_id
            else "chiyun-synchronous"
        )
        return ProviderSubmission(
            provider="chiyun",
            provider_task_id=provider_task_id,
            status="succeeded",
            result_items=results,
        )

    async def poll(
        self,
        submission: ProviderSubmission,
    ) -> ProviderSubmission:
        if submission.provider != "chiyun" or submission.status != "succeeded":
            raise self._error(
                ErrorCategory.VALIDATION,
                "Chiyun 同步任务只能轮询已成功的提交",
                "operation=poll; cause=nonterminal_submission",
            )
        return submission

    async def probe_models(self) -> ModelProbeResult:
        payload = await self._request_json(
            "GET",
            f"{self._base_url}/v1beta/models",
            json_body=None,
            operation="probe_models",
            unsupported_statuses=frozenset({404, 405, 501}),
        )
        if payload is None:
            return self._unsupported_probe()
        models = payload.get("models")
        if not isinstance(models, list):
            return self._unsupported_probe()
        model_ids: list[str] = []
        for item in models:
            if not isinstance(item, dict):
                continue
            value = item.get("name") or item.get("id")
            if not isinstance(value, str) or not value.strip():
                continue
            model_id = value.strip()
            if model_id.startswith("models/"):
                model_id = model_id.removeprefix("models/")
            if model_id and model_id not in model_ids:
                model_ids.append(model_id)
        if models and not model_ids:
            return self._unsupported_probe()
        configured = self._model.removeprefix("models/")
        return ModelProbeResult(
            status="available",
            model_ids=model_ids,
            configured_model_available=configured in model_ids,
            message="已通过只读模型列表接口完成检查",
        )

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | None,
        operation: str,
        unsupported_statuses: frozenset[int] = frozenset(),
    ) -> dict[str, Any] | None:
        try:
            async with self._http_client.stream(
                method,
                url,
                headers={
                    "Authorization": (
                        "Bearer " + self._api_key.get_secret_value()
                    )
                },
                json=json_body,
                timeout=self._timeout,
            ) as response:
                if response.status_code in unsupported_statuses:
                    return None
                if not 200 <= response.status_code < 300:
                    raise self._http_error(operation, response.status_code)
                declared_size = response.headers.get("content-length")
                if declared_size is not None:
                    try:
                        if int(declared_size) > self._max_response_bytes:
                            raise self._provider_error(
                                "Chiyun 响应超过大小限制",
                                f"operation={operation}; cause=response_too_large",
                            )
                    except ValueError:
                        pass
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(raw) + len(chunk) > self._max_response_bytes:
                        raise self._provider_error(
                            "Chiyun 响应超过大小限制",
                            f"operation={operation}; cause=response_too_large",
                        )
                    raw.extend(chunk)
        except AgentError:
            raise
        except httpx.TransportError as exc:
            raise AgentError(
                ErrorDetail(
                    category=ErrorCategory.TRANSIENT,
                    message="连接 Chiyun 服务失败，请稍后重试",
                    technical_detail=(
                        f"operation={operation}; cause={type(exc).__name__}"
                    ),
                    retryable=True,
                )
            ) from None

        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise self._provider_error(
                "Chiyun 返回了无法解析的响应",
                f"operation={operation}; cause=invalid_json",
            ) from None
        if not isinstance(payload, dict):
            raise self._provider_error(
                "Chiyun 返回的数据结构无效",
                f"operation={operation}; cause=non_object_json",
            )
        return payload

    @staticmethod
    def _unsupported_probe() -> ModelProbeResult:
        return ModelProbeResult(
            status="unsupported",
            model_ids=[],
            configured_model_available=None,
            message="该通道无法无费用验证模型；未发起生成请求",
        )

    def _validate_assets(
        self,
        task: GenerationTask,
        assets: list[MediaAsset],
    ) -> list[bytes]:
        if task.task_type.value != "image_to_image":
            raise self._validation_error(
                task.task_id,
                "Chiyun 只接受图生图任务",
                "cause=unsupported_task_type",
            )
        references = task.reference_images
        if not references or not assets:
            raise self._validation_error(
                task.task_id,
                "图生图任务至少需要一张参考图片",
                "cause=missing_reference",
            )
        reference_ids = [reference.asset_id for reference in references]
        reference_orders = [reference.order for reference in references]
        if len(reference_ids) != len(set(reference_ids)):
            raise self._validation_error(
                task.task_id,
                "任务重复引用了同一图片",
                "cause=duplicate_asset_id",
            )
        if reference_orders != list(range(1, len(references) + 1)):
            raise self._validation_error(
                task.task_id,
                "参考图片顺序必须从 1 连续递增",
                "cause=order_mismatch",
            )
        if any(reference.role != "reference_image" for reference in references):
            raise self._validation_error(
                task.task_id,
                "图生图任务只接受普通参考图",
                "cause=invalid_role",
            )
        asset_ids = [asset.asset_id for asset in assets]
        if len(asset_ids) != len(set(asset_ids)):
            raise self._validation_error(
                task.task_id,
                "传入素材包含重复图片",
                "cause=duplicate_input_asset",
            )
        if asset_ids != reference_ids:
            raise self._validation_error(
                task.task_id,
                "传入素材与任务引用或顺序不一致",
                "cause=asset_order_mismatch",
            )

        contents: list[bytes] = []
        for asset in assets:
            if asset.mime_type not in _IMAGE_MIME_TYPES:
                raise self._validation_error(
                    task.task_id,
                    "参考素材不是支持的图片格式",
                    f"asset_id={asset.asset_id}; cause=invalid_mime",
                )
            if asset.download_error is not None:
                raise self._document_error(
                    asset.asset_id,
                    "参考图片下载失败",
                    "cause=download_error",
                )
            if asset.size <= 0 or not asset.sha256:
                raise self._document_error(
                    asset.asset_id,
                    "参考图片元数据无效",
                    "cause=invalid_metadata",
                )
            try:
                content = asset.local_path.read_bytes()
            except OSError as exc:
                raise self._document_error(
                    asset.asset_id,
                    "无法读取参考图片",
                    f"cause={type(exc).__name__}",
                ) from None
            if (
                len(content) != asset.size
                or sha256(content).hexdigest() != asset.sha256
            ):
                raise self._document_error(
                    asset.asset_id,
                    "参考图片完整性校验失败",
                    "cause=content_mismatch",
                )
            contents.append(content)
        return contents

    @staticmethod
    def _prompt(task: GenerationTask) -> str:
        lines = [task.prompt]
        if task.negative_constraints:
            lines.append("必须避免：" + "；".join(task.negative_constraints))
        if task.output_count > 1:
            lines.append(f"请生成 {task.output_count} 张结果图片。")
        return "\n\n".join(lines)

    def _result_items(self, payload: dict[str, Any]) -> list[ProviderResult]:
        results: list[ProviderResult] = []
        candidates = payload.get("candidates")
        if not isinstance(candidates, list):
            raise self._provider_error(
                "Chiyun 返回的数据结构无效",
                "operation=generate; cause=invalid_candidates",
            )
        for candidate in candidates:
            if not isinstance(candidate, dict):
                raise self._provider_error(
                    "Chiyun 返回的数据结构无效",
                    "operation=generate; cause=invalid_candidate",
                )
            content = candidate.get("content")
            if not isinstance(content, dict):
                continue
            parts = content.get("parts")
            if not isinstance(parts, list):
                raise self._provider_error(
                    "Chiyun 返回的数据结构无效",
                    "operation=generate; cause=invalid_parts",
                )
            for part in parts:
                if not isinstance(part, dict):
                    raise self._provider_error(
                        "Chiyun 返回的数据结构无效",
                        "operation=generate; cause=invalid_part",
                    )
                inline = part.get("inlineData")
                if inline is None:
                    inline = part.get("inline_data")
                if inline is not None:
                    results.append(self._inline_result(inline))
                    continue
                file_data = part.get("fileData")
                if file_data is None:
                    file_data = part.get("file_data")
                if file_data is not None:
                    results.append(self._url_result(file_data))
                    continue
                if "url" in part or "image_url" in part:
                    results.append(self._url_result(part))
        return results

    def _inline_result(self, value: Any) -> ProviderResult:
        if not isinstance(value, dict):
            raise self._invalid_result("inline_not_object")
        mime_type = value.get("mimeType") or value.get("mime_type")
        self._validate_result_mime(mime_type)
        data = value.get("data")
        if not isinstance(data, str) or not data or data.startswith("data:"):
            raise self._invalid_result("invalid_inline_data")
        padded = data + "=" * (-len(data) % 4)
        try:
            decoded = base64.b64decode(padded, validate=True)
        except (binascii.Error, ValueError):
            raise self._invalid_result("invalid_base64") from None
        if not decoded:
            raise self._invalid_result("empty_decoded_result")
        if len(decoded) > self._max_result_bytes:
            raise self._invalid_result("decoded_result_too_large")
        return ProviderResult(base64_data=data, mime_type=mime_type)

    def _url_result(self, value: Any) -> ProviderResult:
        if not isinstance(value, dict):
            raise self._invalid_result("url_not_object")
        mime_type = value.get("mimeType") or value.get("mime_type")
        self._validate_result_mime(mime_type)
        url = value.get("fileUri") or value.get("file_uri") or value.get("url")
        image_url = value.get("image_url")
        if url is None and isinstance(image_url, str):
            url = image_url
        elif url is None and isinstance(image_url, dict):
            url = image_url.get("url")
        if not isinstance(url, str):
            raise self._invalid_result("missing_url")
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise self._invalid_result("unsafe_url")
        return ProviderResult(url=url, mime_type=mime_type)

    def _validate_result_mime(self, value: Any) -> None:
        if not isinstance(value, str) or value not in _IMAGE_MIME_TYPES:
            raise self._invalid_result("invalid_mime")

    def _invalid_result(self, cause: str) -> AgentError:
        return self._provider_error(
            "Chiyun 图片结果无效",
            f"operation=generate; cause={cause}",
        )

    @staticmethod
    def _http_error(operation: str, status_code: int) -> AgentError:
        if status_code in {401, 403}:
            category = ErrorCategory.PERMISSION
            message = "Chiyun 凭证无效或没有模型权限"
            retryable = False
        elif status_code == 429 or status_code >= 500:
            category = ErrorCategory.TRANSIENT
            message = "Chiyun 服务暂时不可用，请稍后重试"
            retryable = True
        else:
            category = ErrorCategory.PROVIDER_TERMINAL
            message = "Chiyun 拒绝了生成请求"
            retryable = False
        return AgentError(
            ErrorDetail(
                category=category,
                message=message,
                technical_detail=(
                    f"operation={operation}; status={status_code}"
                ),
                retryable=retryable,
            )
        )

    @staticmethod
    def _provider_error(message: str, technical_detail: str) -> AgentError:
        return AgentError(
            ErrorDetail(
                category=ErrorCategory.PROVIDER_TERMINAL,
                message=message,
                technical_detail=technical_detail,
                retryable=False,
            )
        )

    @staticmethod
    def _error(
        category: ErrorCategory,
        message: str,
        technical_detail: str,
    ) -> AgentError:
        return AgentError(
            ErrorDetail(
                category=category,
                message=message,
                technical_detail=technical_detail,
                retryable=False,
            )
        )

    @classmethod
    def _validation_error(
        cls,
        task_id: str,
        message: str,
        detail: str,
    ) -> AgentError:
        return cls._error(
            ErrorCategory.VALIDATION,
            f"{message}（task_id={task_id}）",
            f"task_id={task_id}; {detail}",
        )

    @classmethod
    def _document_error(
        cls,
        asset_id: str,
        message: str,
        detail: str,
    ) -> AgentError:
        return cls._error(
            ErrorCategory.DOCUMENT,
            f"{message}（asset_id={asset_id}）",
            f"asset_id={asset_id}; {detail}",
        )
