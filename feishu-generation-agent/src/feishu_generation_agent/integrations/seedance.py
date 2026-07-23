import base64
from hashlib import sha256
import ipaddress
from io import BytesIO
import json
import os
import stat
from typing import Any
import unicodedata
from urllib.parse import quote, urlsplit

import httpx
from PIL import Image, UnidentifiedImageError
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
from feishu_generation_agent.domain.plan import GenerationTask
from feishu_generation_agent.integrations.public_media import (
    PublicMediaHost,
    PublicMediaUploadError,
)


_IMAGE_MIME_TYPES = frozenset(
    {"image/gif", "image/jpeg", "image/png", "image/webp"}
)
_IMAGE_FORMAT_MIME_TYPES = {
    "GIF": "image/gif",
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
}
_REFERENCE_ROLES = frozenset(
    {
        "reference_image",
        "first_frame",
        "last_frame",
        "reference_video",
        "reference_audio",
    }
)
_ASPECT_RATIOS = frozenset(
    {"16:9", "9:16", "1:1", "4:3", "3:4", "21:9", "9:21", "adaptive"}
)
_RESOLUTIONS = frozenset({"720p", "1080p"})
_DEFAULT_MAX_RESPONSE_BYTES = 1024 * 1024
_DEFAULT_MAX_INPUT_BYTES = 32 * 1024 * 1024
_DEFAULT_MAX_TOTAL_INPUT_BYTES = 64 * 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024
_NONTERMINAL_STATUSES = frozenset(
    {"queued", "running", "pending", "submitted", "processing", "in_progress"}
)
_TERMINAL_FAILURE_STATUSES = frozenset(
    {"failed", "failure", "cancelled", "canceled", "expired"}
)


class SeedanceVideoGenerator:
    def __init__(
        self,
        http_client: httpx.AsyncClient,
        *,
        base_url: str | None,
        api_key: str | SecretStr | None,
        model: str | None,
        max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
        max_input_bytes: int = _DEFAULT_MAX_INPUT_BYTES,
        max_total_input_bytes: int = _DEFAULT_MAX_TOTAL_INPUT_BYTES,
        public_media_host: PublicMediaHost | None = None,
    ) -> None:
        if not isinstance(base_url, str):
            raise self._configuration_error("base_url", "expected=https_origin_or_api_v3")
        try:
            parsed = urlsplit(base_url.strip())
            port = parsed.port
        except (TypeError, ValueError):
            raise self._configuration_error(
                "base_url", "expected=https_origin_or_api_v3"
            ) from None
        normalized_path = parsed.path.rstrip("/")
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or port not in {None, 443}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or normalized_path not in {"", "/api/v3"}
            or self._is_obviously_local_host(parsed.hostname)
        ):
            raise self._configuration_error(
                "base_url", "expected=https_origin_or_api_v3"
            )

        secret = (
            api_key.get_secret_value()
            if isinstance(api_key, SecretStr)
            else api_key
        )
        if not isinstance(secret, str) or not secret.strip():
            raise self._configuration_error("api_key", "cause=empty")
        if not isinstance(model, str) or not model.strip():
            raise self._configuration_error("model", "cause=empty")

        limits = {
            "max_response_bytes": max_response_bytes,
            "max_input_bytes": max_input_bytes,
            "max_total_input_bytes": max_total_input_bytes,
        }
        for field_name, value in limits.items():
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise self._configuration_error(field_name, "cause=not_positive")
        if max_total_input_bytes < max_input_bytes:
            raise self._configuration_error(
                "max_total_input_bytes", "cause=less_than_single_limit"
            )

        self._http_client = http_client
        self._base_url = f"https://{parsed.netloc}/api/v3"
        self._api_key = SecretStr(secret.strip())
        self._model = model.strip()
        self._max_response_bytes = max_response_bytes
        self._max_input_bytes = max_input_bytes
        self._max_total_input_bytes = max_total_input_bytes
        self._timeout = httpx.Timeout(120, connect=10)
        self._public_media_host = public_media_host

    async def submit(
        self,
        task: GenerationTask,
        assets: list[MediaAsset],
        *,
        submission_id: str | None = None,
    ) -> ProviderSubmission:
        # This is a local crash-correlation token owned by the orchestration layer.
        # Ark does not support client-assigned task IDs, so it must not cross the API.
        del submission_id
        references, ordered_assets, contents = self._validate_submission(task, assets)
        request_content: list[dict[str, Any]] = [
            {"type": "text", "text": self._prompt(task, references)}
        ]
        for reference, asset, content in zip(
            references, ordered_assets, contents, strict=True
        ):
            if asset.mime_type.startswith("image/"):
                encoded = base64.b64encode(content).decode("ascii")
                request_content.append({"type": "image_url", "image_url": {"url": f"data:{asset.mime_type};base64,{encoded}"}, "role": reference.role})
                continue
            if self._public_media_host is None:
                raise self._validation_error(task.task_id, "未配置参考音视频临时托管", "cause=missing_public_media_host")
            try:
                url = await self._public_media_host.upload(content, asset.local_path.name, asset.mime_type)
            except PublicMediaUploadError as exc:
                raise self._error(ErrorCategory.PROVIDER_TRANSIENT, str(exc), "operation=public_media_upload") from None
            request_content.append({
                "type": "video_url" if reference.role == "reference_video" else "audio_url",
                "video_url" if reference.role == "reference_video" else "audio_url": {"url": url},
                "role": reference.role,
            })
        payload = {
            "model": self._model,
            "content": request_content,
            "duration": task.duration,
            "ratio": task.aspect_ratio,
            "resolution": task.resolution,
            "generate_audio": bool(task.generate_audio),
            "watermark": False,
        }
        body = await self._request_json(
            "POST",
            f"{self._base_url}/contents/generations/tasks",
            json_body=payload,
            operation="submit",
        )
        provider_task_id = self._official_task_id(body.get("id"), "submit")
        status = self._status(body.get("status", "queued"), "submit")
        if status in _TERMINAL_FAILURE_STATUSES:
            raise self._terminal_status_error("submit", status)
        result_items = (
            [self._video_result(body, operation="submit")]
            if status in {"success", "succeeded"}
            else []
        )
        return ProviderSubmission(
            provider="seedance",
            provider_task_id=provider_task_id,
            status="succeeded" if status == "success" else status,
            result_items=result_items,
        )

    async def poll(
        self,
        submission: ProviderSubmission,
    ) -> ProviderSubmission:
        if submission.provider != "seedance":
            raise self._error(
                ErrorCategory.VALIDATION,
                "只能轮询 Seedance 提交",
                "operation=poll; cause=provider_mismatch",
            )
        try:
            provider_task_id = self._official_task_id(
                submission.provider_task_id, "poll"
            )
        except AgentError:
            raise self._error(
                ErrorCategory.VALIDATION,
                "Seedance 任务 ID 无效",
                "operation=poll; cause=invalid_provider_task_id",
            ) from None

        task_path = quote(provider_task_id, safe="")
        body = await self._request_json(
            "GET",
            f"{self._base_url}/contents/generations/tasks/{task_path}",
            json_body=None,
            operation="poll",
        )
        returned_id = body.get("id")
        if returned_id is not None:
            official_returned_id = self._official_task_id(returned_id, "poll")
            if official_returned_id != provider_task_id:
                raise self._provider_error(
                    "Seedance 返回了不匹配的任务 ID",
                    "operation=poll; cause=provider_task_id_mismatch",
                )
        status = self._status(body.get("status"), "poll")
        if status in _NONTERMINAL_STATUSES:
            return ProviderSubmission(
                provider="seedance",
                provider_task_id=provider_task_id,
                status=status,
            )
        if status in _TERMINAL_FAILURE_STATUSES:
            raise self._terminal_status_error("poll", status)
        if status not in {"succeeded", "success"}:
            raise self._provider_error(
                "Seedance 返回了未知任务状态",
                "operation=poll; cause=invalid_status",
            )

        result = self._video_result(body, operation="poll")
        return ProviderSubmission(
            provider="seedance",
            provider_task_id=provider_task_id,
            status="succeeded",
            result_items=[result],
        )

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | None,
        operation: str,
    ) -> dict[str, Any]:
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
                follow_redirects=False,
            ) as response:
                if not 200 <= response.status_code < 300:
                    raise self._http_error(operation, response.status_code)
                declared_size = response.headers.get("content-length")
                if declared_size is not None:
                    try:
                        if int(declared_size) > self._max_response_bytes:
                            raise self._provider_error(
                                "Seedance 响应超过大小限制",
                                f"operation={operation}; cause=response_too_large",
                            )
                    except ValueError:
                        pass
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(raw) + len(chunk) > self._max_response_bytes:
                        raise self._provider_error(
                            "Seedance 响应超过大小限制",
                            f"operation={operation}; cause=response_too_large",
                        )
                    raw.extend(chunk)
        except AgentError:
            raise
        except httpx.TransportError as exc:
            raise AgentError(
                ErrorDetail(
                    category=ErrorCategory.TRANSIENT,
                    message="连接 Seedance 服务失败，请稍后重试",
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
                "Seedance 返回了无法解析的响应",
                f"operation={operation}; cause=invalid_json",
            ) from None
        if not isinstance(payload, dict):
            raise self._provider_error(
                "Seedance 返回的数据结构无效",
                f"operation={operation}; cause=non_object_json",
            )
        return payload

    def _video_result(
        self,
        body: dict[str, Any],
        *,
        operation: str,
    ) -> ProviderResult:
        candidates = self._collect_video_url_candidates(
            body,
            operation=operation,
        )
        video_urls: list[str] = []
        for candidate in candidates:
            if not isinstance(candidate, str) or not candidate:
                raise self._invalid_result(operation, "invalid_video_url")
            self._validate_result_url(candidate, operation=operation)
            if candidate not in video_urls:
                video_urls.append(candidate)
        if not video_urls:
            raise self._invalid_result(operation, "missing_video_url")
        if len(video_urls) != 1:
            raise self._invalid_result(operation, "multiple_video_urls")
        return ProviderResult(
            url=video_urls[0],
            url_trust="untrusted",
            mime_type="video/mp4",
        )

    def _collect_video_url_candidates(
        self,
        body: dict[str, Any],
        *,
        operation: str,
        depth: int = 0,
    ) -> list[Any]:
        if depth > 16:
            raise self._invalid_result(operation, "nested_data_too_deep")
        candidates: list[Any] = []
        content = body.get("content")
        if isinstance(content, dict):
            mime_type = content.get("mime_type")
            if mime_type is not None and mime_type != "video/mp4":
                raise self._invalid_result(operation, "invalid_mime")
            self._append_mapping_values(
                candidates,
                content,
                ("video_url", "videoUrl"),
            )
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict):
                    self._append_mapping_values(
                        candidates,
                        item,
                        ("video_url", "videoUrl"),
                        unwrap_url_object=True,
                    )
                    if item.get("type") == "video_url":
                        self._append_mapping_values(
                            candidates,
                            item,
                            ("url",),
                        )

        nested = body.get("data")
        if isinstance(nested, dict):
            candidates.extend(
                self._collect_video_url_candidates(
                    nested,
                    operation=operation,
                    depth=depth + 1,
                )
            )
        self._append_mapping_values(
            candidates,
            body,
            ("video_url", "videoUrl"),
        )
        results = body.get("results")
        if isinstance(results, list):
            for item in results:
                if isinstance(item, dict):
                    self._append_mapping_values(
                        candidates,
                        item,
                        ("url", "video_url"),
                    )
        return candidates

    @staticmethod
    def _append_mapping_values(
        candidates: list[Any],
        mapping: dict[str, Any],
        keys: tuple[str, ...],
        *,
        unwrap_url_object: bool = False,
    ) -> None:
        for key in keys:
            if key not in mapping:
                continue
            value = mapping[key]
            if unwrap_url_object and isinstance(value, dict):
                value = value.get("url")
            candidates.append(value)

    def _validate_result_url(self, value: str, *, operation: str) -> None:
        if value != value.strip() or len(value) > 8192:
            raise self._invalid_result(operation, "unsafe_video_url")
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except (TypeError, ValueError):
            raise self._invalid_result(operation, "unsafe_video_url") from None
        hostname = (parsed.hostname or "").rstrip(".").lower()
        if (
            parsed.scheme != "https"
            or not hostname
            or port not in {None, 443}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or self._is_obviously_local_host(hostname)
        ):
            raise self._invalid_result(operation, "unsafe_video_url")

    @staticmethod
    def _is_obviously_local_host(value: str | None) -> bool:
        hostname = (value or "").rstrip(".").lower()
        if (
            not hostname
            or hostname == "localhost"
            or hostname.endswith(".localhost")
            or hostname.endswith(".local")
            or hostname.endswith(".internal")
        ):
            return True
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            return False
        return not address.is_global

    def _official_task_id(self, value: Any, operation: str) -> str:
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or len(value) > 512
            or any(
                unicodedata.category(character) == "Cc"
                for character in value
            )
        ):
            raise self._provider_error(
                "Seedance 返回的任务 ID 无效",
                f"operation={operation}; cause=invalid_provider_task_id",
            )
        return value

    def _status(self, value: Any, operation: str) -> str:
        if not isinstance(value, str) or value not in (
            _NONTERMINAL_STATUSES
            | _TERMINAL_FAILURE_STATUSES
            | {"succeeded", "success"}
        ):
            raise self._provider_error(
                "Seedance 返回了未知任务状态",
                f"operation={operation}; cause=invalid_status",
            )
        return value

    def _validate_submission(
        self,
        task: GenerationTask,
        assets: list[MediaAsset],
    ) -> tuple[list[Any], list[MediaAsset], list[bytes]]:
        self._validate_video_parameters(task)
        references = sorted(task.reference_images, key=lambda item: item.order)
        if not references or not assets:
            raise self._validation_error(
                task.task_id,
                "图生视频任务至少需要一张参考图片",
                "cause=missing_reference",
            )

        reference_ids = [reference.asset_id for reference in references]
        reference_orders = [reference.order for reference in references]
        if len(reference_ids) != len(set(reference_ids)):
            raise self._validation_error(
                task.task_id,
                "任务重复引用了同一图片",
                "cause=duplicate_reference",
            )
        if reference_orders != list(range(1, len(references) + 1)):
            raise self._validation_error(
                task.task_id,
                "参考图片顺序必须从 1 连续递增",
                "cause=order_mismatch",
            )
        if any(reference.role not in _REFERENCE_ROLES for reference in references):
            raise self._validation_error(
                task.task_id,
                "参考图片角色不受支持",
                "cause=invalid_role",
            )
        self._validate_reference_roles(task, references)

        asset_ids = [asset.asset_id for asset in assets]
        if len(asset_ids) != len(set(asset_ids)):
            raise self._validation_error(
                task.task_id,
                "传入素材包含重复图片",
                "cause=duplicate_input_asset",
            )
        if set(asset_ids) != set(reference_ids):
            raise self._validation_error(
                task.task_id,
                "传入素材与任务引用不一致",
                "cause=asset_mapping_mismatch",
            )
        by_id = {asset.asset_id: asset for asset in assets}
        ordered_assets = [by_id[asset_id] for asset_id in reference_ids]

        expected_stats: list[os.stat_result] = []
        total_size = 0
        for reference, asset in zip(references, ordered_assets, strict=True):
            valid_role = (
                (asset.mime_type in _IMAGE_MIME_TYPES and reference.role in {"reference_image", "first_frame", "last_frame"})
                or (asset.mime_type.startswith("video/") and reference.role == "reference_video")
                or (asset.mime_type.startswith("audio/") and reference.role == "reference_audio")
            )
            if not valid_role:
                raise self._validation_error(
                    task.task_id,
                    "参考素材类型与用途不匹配",
                    f"asset_id={asset.asset_id}; cause=invalid_mime_or_role",
                )
            if asset.download_error is not None:
                raise self._document_error(
                    asset.asset_id,
                    "参考图片下载失败",
                    "cause=download_error",
                )
            if (
                asset.size <= 0
                or not isinstance(asset.sha256, str)
                or len(asset.sha256) != 64
            ):
                raise self._document_error(
                    asset.asset_id,
                    "参考图片元数据无效",
                    "cause=invalid_metadata",
                )
            try:
                file_stat = asset.local_path.lstat()
            except OSError as exc:
                raise self._document_error(
                    asset.asset_id,
                    "无法读取参考图片",
                    f"cause={type(exc).__name__}",
                ) from None
            if not stat.S_ISREG(file_stat.st_mode):
                raise self._document_error(
                    asset.asset_id,
                    "参考图片路径不安全",
                    "cause=unsafe_file",
                )
            if file_stat.st_size > self._max_input_bytes:
                raise self._document_error(
                    asset.asset_id,
                    "参考图片超过大小限制",
                    "cause=input_too_large",
                )
            if file_stat.st_size != asset.size:
                raise self._document_error(
                    asset.asset_id,
                    "参考图片完整性校验失败",
                    "cause=content_mismatch",
                )
            total_size += file_stat.st_size
            if total_size > self._max_total_input_bytes:
                raise self._document_error(
                    asset.asset_id,
                    "参考图片总量超过大小限制",
                    "cause=total_input_too_large",
                )
            expected_stats.append(file_stat)

        contents: list[bytes] = []
        for asset, expected_stat in zip(
            ordered_assets, expected_stats, strict=True
        ):
            content = self._read_verified_asset(asset, expected_stat)
            if asset.mime_type.startswith("image/"):
                self._validate_image_content(asset, content)
            contents.append(content)
        return references, ordered_assets, contents

    def _validate_video_parameters(self, task: GenerationTask) -> None:
        if task.task_type.value != "image_to_video":
            raise self._validation_error(
                task.task_id,
                "Seedance 只接受图生视频任务",
                "cause=unsupported_task_type",
            )
        if (
            not isinstance(task.duration, int)
            or isinstance(task.duration, bool)
            or not 4 <= task.duration <= 15
        ):
            raise self._validation_error(
                task.task_id, "视频时长无效", "cause=invalid_duration"
            )
        if task.aspect_ratio not in _ASPECT_RATIOS:
            raise self._validation_error(
                task.task_id, "视频比例无效", "cause=invalid_aspect_ratio"
            )
        if task.resolution not in _RESOLUTIONS:
            raise self._validation_error(
                task.task_id, "视频分辨率无效", "cause=invalid_resolution"
            )
        if task.generate_audio is not None and not isinstance(
            task.generate_audio, bool
        ):
            raise self._validation_error(
                task.task_id, "音频参数无效", "cause=invalid_generate_audio"
            )
        if task.output_count != 1:
            raise self._validation_error(
                task.task_id, "Seedance 每次只生成一个视频", "cause=output_count"
            )

    def _validate_reference_roles(
        self, task: GenerationTask, references: list[Any]
    ) -> None:
        roles = [reference.role for reference in references]
        if task.reference_mode == "first_last_frame":
            if roles != ["first_frame", "last_frame"]:
                raise self._validation_error(
                    task.task_id,
                    "首尾帧模式必须且只能按顺序指定一张首帧和一张尾帧",
                    "cause=invalid_first_last_frame_mode",
                )
            return
        if task.reference_mode == "multi_reference":
            if any(role in {"first_frame", "last_frame"} for role in roles):
                raise self._validation_error(
                    task.task_id,
                    "多参考模式不能使用首帧或尾帧",
                    "cause=invalid_multi_reference_mode",
                )
            return
        if "reference_image" in roles and any(
            role in {"first_frame", "last_frame"} for role in roles
        ):
            raise self._validation_error(
                task.task_id,
                "普通参考图不能与首尾帧混用",
                "cause=mixed_reference_roles",
            )
        if roles.count("first_frame") > 1 or roles.count("last_frame") > 1:
            raise self._validation_error(
                task.task_id,
                "首帧或尾帧只能各指定一张",
                "cause=duplicate_frame_role",
            )
        if "last_frame" in roles and "first_frame" not in roles:
            raise self._validation_error(
                task.task_id,
                "指定尾帧时必须同时指定首帧",
                "cause=last_frame_without_first",
            )
        if "first_frame" in roles and "last_frame" in roles:
            if roles.index("first_frame") > roles.index("last_frame"):
                raise self._validation_error(
                    task.task_id,
                    "首帧必须排在尾帧之前",
                    "cause=frame_order",
                )

    def _read_verified_asset(
        self,
        asset: MediaAsset,
        expected_stat: os.stat_result,
    ) -> bytes:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(
            os, "O_NOFOLLOW", 0
        )
        try:
            descriptor = os.open(asset.local_path, flags)
        except OSError as exc:
            raise self._document_error(
                asset.asset_id,
                "无法读取参考图片",
                f"cause={type(exc).__name__}",
            ) from None
        try:
            opened_stat = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened_stat.st_mode)
                or self._file_identity(opened_stat)
                != self._file_identity(expected_stat)
            ):
                raise self._document_error(
                    asset.asset_id,
                    "参考图片路径不安全",
                    "cause=file_replaced",
                )
            content = bytearray()
            digest = sha256()
            while True:
                chunk = os.read(descriptor, _READ_CHUNK_BYTES)
                if not chunk:
                    break
                if len(content) + len(chunk) > self._max_input_bytes:
                    raise self._document_error(
                        asset.asset_id,
                        "参考图片超过大小限制",
                        "cause=input_too_large",
                    )
                content.extend(chunk)
                digest.update(chunk)
            final_stat = os.fstat(descriptor)
        except AgentError:
            raise
        except OSError as exc:
            raise self._document_error(
                asset.asset_id,
                "无法读取参考图片",
                f"cause={type(exc).__name__}",
            ) from None
        finally:
            os.close(descriptor)

        try:
            path_stat = asset.local_path.lstat()
        except OSError as exc:
            raise self._document_error(
                asset.asset_id,
                "参考图片路径在读取期间发生变化",
                f"cause={type(exc).__name__}",
            ) from None
        if (
            self._file_identity(final_stat) != self._file_identity(expected_stat)
            or self._file_identity(path_stat) != self._file_identity(expected_stat)
            or len(content) != asset.size
            or digest.hexdigest() != asset.sha256
        ):
            raise self._document_error(
                asset.asset_id,
                "参考图片完整性校验失败",
                "cause=content_mismatch",
            )
        return bytes(content)

    @staticmethod
    def _file_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
        return (
            value.st_dev,
            value.st_ino,
            value.st_mode,
            value.st_size,
            value.st_mtime_ns,
        )

    def _validate_image_content(self, asset: MediaAsset, content: bytes) -> None:
        try:
            with Image.open(BytesIO(content)) as image:
                image_format = image.format
                image.verify()
        except (UnidentifiedImageError, OSError, ValueError):
            raise self._document_error(
                asset.asset_id,
                "参考图片内容无效",
                "cause=invalid_image_content",
            ) from None
        actual_mime = _IMAGE_FORMAT_MIME_TYPES.get(str(image_format).upper())
        if actual_mime != asset.mime_type:
            raise self._document_error(
                asset.asset_id,
                "参考图片格式与声明不一致",
                "cause=mime_mismatch",
            )

    @staticmethod
    def _prompt(task: GenerationTask, references: list[Any]) -> str:
        lines = [task.prompt]
        lines.append(
            "参考图映射："
            + "；".join(
                f"图片 {index}={reference.role}"
                for index, reference in enumerate(references, start=1)
            )
        )
        if task.negative_constraints:
            lines.append("必须避免：" + "；".join(task.negative_constraints))
        return "\n\n".join(lines)

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
    def _configuration_error(cls, field_name: str, cause: str) -> AgentError:
        messages = {
            "base_url": "Seedance 服务地址配置无效",
            "api_key": "Seedance API Key 未配置",
            "model": "Seedance 模型未配置",
        }
        return cls._error(
            ErrorCategory.CONFIGURATION,
            messages.get(field_name, "Seedance 大小限制配置无效"),
            f"field={field_name}; {cause}",
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

    @staticmethod
    def _http_error(operation: str, status_code: int) -> AgentError:
        if status_code in {401, 403}:
            category = ErrorCategory.PERMISSION
            message = "Seedance 凭证无效或没有模型权限"
            retryable = False
        elif status_code == 429 or status_code >= 500:
            category = ErrorCategory.TRANSIENT
            message = "Seedance 服务暂时不可用，请稍后重试"
            retryable = True
        else:
            category = ErrorCategory.PROVIDER_TERMINAL
            message = "Seedance 拒绝了请求"
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

    def _terminal_status_error(self, operation: str, status: str) -> AgentError:
        return self._provider_error(
            "Seedance 视频任务未成功完成",
            f"operation={operation}; status={status}",
        )

    def _invalid_result(self, operation: str, cause: str) -> AgentError:
        return self._provider_error(
            "Seedance 视频结果无效",
            f"operation={operation}; cause={cause}",
        )
