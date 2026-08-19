"""Seedream 5.0 Pro（火山方舟图片生成）。

复用策略——不重复造轮子：
- 传输层直接用 SeedanceVideoGenerator._request_json：seedream 与 seedance
  同属火山方舟，共用 ark_base_url、ark_api_key 和 Bearer 鉴权，那份实现
  已经带流式读取、响应体积上限、provider 错误码提取和 TransportError 分类。
- 结果落盘用 ProviderResultStore，与 ChiyunImageGenerator 同一套，产出
  的 ProviderResult 形状一致，下游 verify/download 节点无需区分 provider。
- 尺寸映射表照搬 nano-banana 子应用实测出的 _SEEDREAM_5_PRO_SIZES，
  不自己推导。

本模块自己只负责：payload 组装 + 结果提取。
"""

import base64
from pathlib import Path
from typing import Any

import httpx
from pydantic import SecretStr

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
from feishu_generation_agent.domain.plan import GenerationTask, TaskType
from feishu_generation_agent.integrations.safe_download import ResultDownloader
from feishu_generation_agent.integrations.seedance import (
    SeedanceVideoGenerator,
)
from feishu_generation_agent.storage.provider_results import (
    ProviderResultStagingError,
    ProviderResultStore,
    StagedProviderResult,
)


_PROVIDER_NAME = "seedream"
_RESULT_MIME = "image/png"
_MAX_REFERENCE_IMAGES = 10

# 来自 nano-banana 子应用的实测约束，勿凭空推导。
_SEEDREAM_5_PRO_SIZES: dict[str, dict[str, str]] = {
    "1K": {
        "1:1": "1024x1024", "4:3": "1152x864", "3:4": "864x1152",
        "16:9": "1424x800", "9:16": "800x1424", "3:2": "1248x832",
        "2:3": "832x1248", "21:9": "1568x672", "9:21": "672x1568",
    },
    "1.5K": {
        "1:1": "1536x1536", "4:3": "1792x1344", "3:4": "1344x1792",
        "16:9": "2048x1152", "9:16": "1152x2048", "3:2": "1872x1248",
        "2:3": "1248x1872", "21:9": "2352x1008", "9:21": "1008x2352",
    },
    "2K": {
        "1:1": "2048x2048", "4:3": "2368x1776", "3:4": "1776x2368",
        "16:9": "2816x1584", "9:16": "1584x2816", "3:2": "2496x1664",
        "2:3": "1664x2496", "21:9": "3136x1344", "9:21": "1344x3136",
    },
}


def seedream_size(resolution: str, aspect_ratio: str) -> str:
    resolution = str(resolution or "2K").strip()
    aspect_ratio = str(aspect_ratio or "auto").strip()
    mapping = _SEEDREAM_5_PRO_SIZES.get(resolution)
    if mapping is None:
        raise ValueError("Seedream 5.0 Pro 尺寸只支持 1K、1.5K、2K")
    if aspect_ratio == "auto":
        return resolution
    if aspect_ratio not in mapping:
        raise ValueError(f"Seedream 5.0 Pro 不支持比例 {aspect_ratio}")
    return mapping[aspect_ratio]


class SeedreamImageGenerator:
    def __init__(
        self,
        http_client: httpx.AsyncClient,
        *,
        base_url: str | None,
        api_key: str | SecretStr | None,
        model: str | None,
        staging_dir: Path,
        result_downloader: ResultDownloader | None,
        max_result_bytes: int,
    ) -> None:
        # 让 seedance 的构造器承担 base_url / api_key / model 的全部校验，
        # 校验规则与既有火山链路保持一致。
        self._transport = SeedanceVideoGenerator(
            http_client,
            base_url=base_url,
            api_key=api_key,
            model=model,
            provider_name=_PROVIDER_NAME,
        )
        self._base_url = self._transport._base_url
        self._model = self._transport._model
        if result_downloader is None:
            raise self._configuration_error(
                "result_downloader", "cause=missing"
            )
        self._result_downloader = result_downloader
        try:
            self._result_store = ProviderResultStore(
                staging_dir,
                max_item_bytes=max_result_bytes,
            )
        except (OSError, ValueError) as error:
            raise self._configuration_error(
                "staging_dir", "cause=unusable"
            ) from error

    async def submit(
        self,
        task: GenerationTask,
        assets: list[MediaAsset],
        *,
        submission_id: str | None = None,
    ) -> ProviderSubmission:
        if task.task_type is not TaskType.IMAGE_TO_IMAGE:
            raise self._provider_error(
                "Seedream 只支持图生图任务",
                f"operation=generate; task_type={task.task_type.value}",
            )

        payload = self._build_payload(task, assets)
        body = await self._transport._request_json(
            "POST",
            f"{self._base_url}/images/generations",
            json_body=payload,
            operation="generate",
        )
        materialized = await self._materialize(body)
        try:
            provider_task_id, staged = self._result_store.save(
                materialized,
                provider_task_id=submission_id,
            )
        except (OSError, ProviderResultStagingError):
            raise self._provider_error(
                "Seedream 图片结果无法安全落盘",
                "operation=generate; cause=staging_write_failed",
            ) from None
        return ProviderSubmission(
            provider=_PROVIDER_NAME,
            provider_task_id=provider_task_id,
            status="succeeded",
            result_items=self._provider_results(staged),
        )

    async def poll(self, submission: ProviderSubmission) -> ProviderSubmission:
        """Seedream 是同步接口，submit 返回即终态。"""
        return submission

    def _build_payload(
        self,
        task: GenerationTask,
        assets: list[MediaAsset],
    ) -> dict[str, Any]:
        try:
            size = seedream_size(task.image_size or "2K", task.aspect_ratio)
        except ValueError as error:
            raise self._provider_error(
                str(error),
                f"operation=generate; image_size={task.image_size!r}; "
                f"aspect_ratio={task.aspect_ratio!r}",
            ) from error

        payload: dict[str, Any] = {
            "model": self._model,
            "prompt": task.prompt,
            "size": size,
            "response_format": "url",
            "output_format": "png",
            "watermark": False,
        }
        images = self._reference_data_urls(task, assets)
        if images:
            payload["image"] = images[0] if len(images) == 1 else images
        return payload

    def _reference_data_urls(
        self,
        task: GenerationTask,
        assets: list[MediaAsset],
    ) -> list[str]:
        by_id = {asset.asset_id: asset for asset in assets}
        urls: list[str] = []
        for reference in sorted(
            task.reference_images, key=lambda item: item.order
        ):
            asset = by_id.get(reference.asset_id)
            if asset is None:
                raise self._provider_error(
                    "Seedream 参考图缺失",
                    f"operation=generate; asset_id={reference.asset_id}",
                )
            try:
                content = asset.local_path.read_bytes()
            except OSError as error:
                raise self._provider_error(
                    "Seedream 参考图读取失败",
                    f"operation=generate; asset_id={reference.asset_id}",
                ) from error
            encoded = base64.b64encode(content).decode("ascii")
            urls.append(f"data:{asset.mime_type};base64,{encoded}")
        if len(urls) > _MAX_REFERENCE_IMAGES:
            raise self._provider_error(
                f"Seedream 最多支持 {_MAX_REFERENCE_IMAGES} 张参考图",
                f"operation=generate; reference_count={len(urls)}",
            )
        return urls

    async def _materialize(
        self, body: dict[str, Any]
    ) -> list[tuple[bytes, str]]:
        items = self._extract_items(body)
        if not items:
            raise self._provider_error(
                "Seedream 未返回图片结果",
                "operation=generate; cause=empty_result",
            )
        materialized: list[tuple[bytes, str]] = []
        for item in items:
            encoded = item.get("b64_json")
            if isinstance(encoded, str) and encoded:
                try:
                    materialized.append(
                        (base64.b64decode(encoded, validate=True), _RESULT_MIME)
                    )
                except (ValueError, TypeError):
                    raise self._provider_error(
                        "Seedream 返回的图片数据无法解码",
                        "operation=generate; cause=invalid_base64",
                    ) from None
                continue
            url = item.get("url")
            if isinstance(url, str) and url:
                materialized.append(
                    (
                        await self._result_downloader.download(
                            url, expected_mime_type=_RESULT_MIME
                        ),
                        _RESULT_MIME,
                    )
                )
                continue
            raise self._provider_error(
                "Seedream 返回结果缺少图片来源",
                "operation=generate; cause=missing_source",
            )
        return materialized

    @staticmethod
    def _extract_items(body: dict[str, Any]) -> list[dict[str, Any]]:
        data = body.get("data")
        if isinstance(data, dict):
            data = data.get("data", data)
        if not isinstance(data, list):
            data = [data] if data else []
        return [item for item in data if isinstance(item, dict)]

    @staticmethod
    def _provider_results(
        staged_results: list[StagedProviderResult],
    ) -> list[ProviderResult]:
        return [
            ProviderResult(
                local_path=result.local_path,
                mime_type=result.mime_type,
                size=result.size,
                sha256=result.sha256,
            )
            for result in staged_results
        ]

    @staticmethod
    def _configuration_error(field_name: str, cause: str) -> AgentError:
        return AgentError(
            ErrorDetail(
                category=ErrorCategory.CONFIGURATION,
                message="Seedream 配置无效",
                technical_detail=f"field={field_name}; {cause}",
                retryable=False,
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
