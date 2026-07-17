import asyncio
import base64
from typing import Any

import httpx
from pydantic import ValidationError

from feishu_generation_agent.domain.document import (
    MediaAsset,
    VisionDescription,
)
from feishu_generation_agent.domain.errors import (
    AgentError,
    ErrorCategory,
    ErrorDetail,
)
from feishu_generation_agent.storage.repository import Repository


_SYSTEM_PROMPT = """你是严格的图片观察与转录工具。
1. 只描述图片中直接可见的内容，不补充图片之外的信息。
2. 不得推断未出现的剧情、品牌或人物身份。
3. visible_text 必须逐项抄录图片中实际可见的文字；看不清时不要猜测。
4. 所有不确定信息只能写入 uncertainties，不得混入其他字段。
5. 严格按给定结构返回结果，不要附加解释或原始响应。
"""


class _ModelRefusal(RuntimeError):
    pass


class _AssetReadFailure(RuntimeError):
    def __init__(self, cause_name: str) -> None:
        super().__init__()
        self.cause_name = cause_name


class ClaudeVisionAnalyzer:
    def __init__(
        self,
        model: Any,
        repository: Repository,
        *,
        prompt_version: str,
        model_name: str | None = None,
    ) -> None:
        self._model = model
        self._repository = repository
        self.prompt_version = prompt_version
        self.model_name = model_name or self._resolve_model_name(model)
        self._inflight: dict[str, asyncio.Task[VisionDescription]] = {}
        self._inflight_lock = asyncio.Lock()

    async def analyze(self, asset: MediaAsset) -> VisionDescription:
        if asset.download_error is not None:
            raise self._download_error(asset)

        cache_key = (
            f"{asset.sha256}:{self.model_name}:{self.prompt_version}"
        )
        cache_error: AgentError | None = None
        try:
            cached = await self._repository.get_vision_cache(cache_key)
            if cached is not None:
                description = VisionDescription.model_validate(cached)
                if description.asset_id != asset.asset_id:
                    description = description.model_copy(
                        update={"asset_id": asset.asset_id}
                    )
                return description
        except Exception as exc:
            cache_error = self._error_for(asset, exc)
        if cache_error is not None:
            raise cache_error

        async with self._inflight_lock:
            pending = self._inflight.get(cache_key)
            if pending is None:
                pending = asyncio.create_task(
                    self._analyze_and_cache(asset, cache_key)
                )
                self._inflight[cache_key] = pending

        shared_description: VisionDescription | None = None
        shared_error: AgentError | None = None
        try:
            shared_description = await asyncio.shield(pending)
        except Exception as exc:
            shared_error = self._error_for(asset, exc)
        finally:
            if pending.done():
                async with self._inflight_lock:
                    if self._inflight.get(cache_key) is pending:
                        self._inflight.pop(cache_key, None)
        if shared_error is not None:
            raise shared_error
        if shared_description is None:
            raise self._error_for(asset, _ModelRefusal())
        return shared_description.model_copy(update={"asset_id": asset.asset_id})

    async def _analyze_and_cache(
        self,
        asset: MediaAsset,
        cache_key: str,
    ) -> VisionDescription:
        read_error: _AssetReadFailure | None = None
        try:
            image_bytes = asset.local_path.read_bytes()
        except OSError as exc:
            read_error = _AssetReadFailure(type(exc).__name__)
        if read_error is not None:
            raise read_error

        image_data = base64.b64encode(image_bytes).decode("ascii")
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": asset.mime_type,
                            "data": image_data,
                        },
                    },
                    {
                        "type": "text",
                        "text": (
                            "请分析这张参考图；asset_id 仅为结构占位字段，"
                            "请设为空字符串。"
                        ),
                    },
                ],
            },
        ]
        structured_model = self._model.with_structured_output(
            VisionDescription
        )
        result = await structured_model.ainvoke(messages)
        if result is None or (isinstance(result, dict) and result.get("refusal")):
            raise _ModelRefusal
        description = VisionDescription.model_validate(result).model_copy(
            update={"asset_id": ""}
        )
        await self._repository.save_vision_cache(cache_key, description)
        return description

    @staticmethod
    def _resolve_model_name(model: Any) -> str:
        for attribute in ("model_name", "model"):
            value = getattr(model, attribute, None)
            if isinstance(value, str) and value.strip():
                return value
        return type(model).__name__

    @classmethod
    def _error_for(cls, asset: MediaAsset, exc: Exception) -> AgentError:
        if isinstance(exc, _AssetReadFailure):
            return cls._asset_read_error(asset, exc.cause_name)

        status_code = cls._status_code(exc)
        exception_name = type(exc).__name__
        lowered_name = exception_name.lower()
        technical_detail = (
            f"asset_id={asset.asset_id}; cause={exception_name}"
        )
        if status_code is not None:
            technical_detail += f"; status={status_code}"

        if (
            status_code == 429
            or (status_code is not None and status_code >= 500)
            or isinstance(
                exc,
                (httpx.TransportError, TimeoutError, ConnectionError),
            )
            or lowered_name in {
                "apiconnectionerror",
                "apitimeouterror",
                "ratelimiterror",
            }
        ):
            return AgentError(
                ErrorDetail(
                    category=ErrorCategory.TRANSIENT,
                    message=(
                        "视觉分析服务暂时不可用"
                        f"（asset_id={asset.asset_id}）"
                    ),
                    technical_detail=technical_detail,
                    retryable=True,
                )
            )

        if "refusal" in lowered_name:
            return AgentError(
                ErrorDetail(
                    category=ErrorCategory.PROVIDER_TERMINAL,
                    message=f"视觉模型拒绝分析素材（asset_id={asset.asset_id}）",
                    technical_detail=technical_detail,
                    retryable=False,
                )
            )

        if isinstance(exc, (ValidationError, ValueError, TypeError)):
            return AgentError(
                ErrorDetail(
                    category=ErrorCategory.VALIDATION,
                    message=(
                        "视觉模型返回的结构无效"
                        f"（asset_id={asset.asset_id}）"
                    ),
                    technical_detail=technical_detail,
                    retryable=False,
                )
            )

        return AgentError(
            ErrorDetail(
                category=ErrorCategory.PROVIDER_TERMINAL,
                message=f"视觉分析失败（asset_id={asset.asset_id}）",
                technical_detail=technical_detail,
                retryable=False,
            )
        )

    @staticmethod
    def _asset_read_error(asset: MediaAsset, cause_name: str) -> AgentError:
        return AgentError(
            ErrorDetail(
                category=ErrorCategory.DOCUMENT,
                message=f"无法读取图片素材（asset_id={asset.asset_id}）",
                technical_detail=(
                    f"asset_id={asset.asset_id}; cause={cause_name}"
                ),
                retryable=False,
            )
        )

    @staticmethod
    def _download_error(asset: MediaAsset) -> AgentError:
        return AgentError(
            ErrorDetail(
                category=ErrorCategory.DOCUMENT,
                message=f"图片素材下载失败（asset_id={asset.asset_id}）",
                technical_detail=(
                    f"asset_id={asset.asset_id}; cause=download_error"
                ),
                retryable=False,
            )
        )

    @staticmethod
    def _status_code(exc: Exception) -> int | None:
        status_code = getattr(exc, "status_code", None)
        if isinstance(status_code, int):
            return status_code
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
        return status_code if isinstance(status_code, int) else None
