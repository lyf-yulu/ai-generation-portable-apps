import json
import re
from typing import Any, Callable

import httpx
from pydantic import BaseModel, ValidationError

from feishu_generation_agent.domain.document import (
    NormalizedDocument,
    VisionDescription,
)
from feishu_generation_agent.domain.errors import (
    AgentError,
    ErrorCategory,
    ErrorDetail,
)
from feishu_generation_agent.domain.plan import AuditReport, TaskPlan


_ALLOWED_TASK_TYPES = {"image_to_image", "image_to_video"}
_STORYBOARD_ROW_MARKER = re.compile(
    r"^\s*镜头\s*(?:\d+|[一二三四五六七八九十百]+)\s*[：:]?"
)
_PLAN_SYSTEM_PROMPT = """你是 AI 图片与视频生成需求规划器。
只根据给定文档、稳定引用和视觉描述输出 TaskPlan JSON，不得虚构素材或需求。
不要输出思维过程、推理原文、Markdown 或 JSON 之外的说明。
"""
_AUDIT_SYSTEM_PROMPT = """你是独立审查员，与需求规划角色相互独立。
只指出计划中的遗漏、冲突、虚构内容和供应商限制，不得改写计划或生成替代任务。
严格输出 AuditReport JSON，不要输出思维过程、推理原文、Markdown 或额外说明。
"""


def _compact_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _storyboard_requirements(
    document: NormalizedDocument,
) -> dict[str, list[str]]:
    """Return content block IDs for explicitly marked storyboard tables.

    A table is treated as a storyboard only when it has at least two non-empty
    rows and every non-empty row contains text beginning with an explicit
    ``镜头 N`` marker. This deliberately excludes ordinary parameter tables.
    """
    requirements: dict[str, list[str]] = {}
    tables = sorted(
        (
            block
            for block in document.blocks
            if block.block_type == "table"
        ),
        key=lambda block: (block.order, block.block_id),
    )
    for table in tables:
        cells = {
            block.block_id: block.table_row
            for block in document.blocks
            if block.block_type == "table_cell"
            and block.parent_id == table.block_id
            and isinstance(block.table_row, int)
        }
        row_content: dict[int, list[Any]] = {}
        for block in document.blocks:
            if block.block_type in {"table", "table_cell"}:
                continue
            row = next(
                (
                    cells[path_part]
                    for path_part in block.path
                    if path_part in cells
                ),
                None,
            )
            if row is not None:
                row_content.setdefault(row, []).append(block)

        if len(row_content) < 2:
            continue
        marker_rows = {
            row
            for row, blocks in row_content.items()
            if any(
                block.block_type == "text"
                and bool(_STORYBOARD_ROW_MARKER.match(block.text))
                for block in blocks
            )
        }
        if marker_rows != set(row_content):
            continue

        content_blocks = sorted(
            (
                block
                for blocks in row_content.values()
                for block in blocks
            ),
            key=lambda block: (block.order, block.block_id),
        )
        requirements[table.block_id] = [
            block.block_id for block in content_blocks
        ]
    return requirements


def validate_plan(
    plan: TaskPlan | dict[str, Any],
    document: NormalizedDocument,
    max_output_count: int,
) -> list[str]:
    payload: Any
    if isinstance(plan, TaskPlan):
        payload = plan.model_dump(mode="json")
    else:
        payload = plan

    issues: list[str] = []
    if not isinstance(payload, dict):
        return ["plan: must be a JSON object"]
    tasks = payload.get("tasks")
    if not isinstance(tasks, list):
        return ["plan.tasks: must be a list"]
    if not tasks:
        return ["plan.tasks: at least one generation task is required"]

    block_ids = {block.block_id for block in document.blocks}
    assets = {asset.asset_id: asset for asset in document.media_assets}
    storyboard_requirements = _storyboard_requirements(document)
    task_ids: set[str] = set()
    total_output_count = 0
    task_sources: list[tuple[str, str | None, set[str]]] = []

    for index, task in enumerate(tasks):
        prefix = f"tasks[{index}]"
        if not isinstance(task, dict):
            issues.append(f"{prefix}: must be a JSON object")
            continue

        task_id = task.get("task_id")
        display_id = task_id if isinstance(task_id, str) and task_id else prefix
        if not isinstance(task_id, str) or not task_id:
            issues.append(f"{prefix}.task_id: must be a non-empty string")
        elif task_id in task_ids:
            issues.append(f"{prefix}.task_id: duplicate task_id {task_id}")
        else:
            task_ids.add(task_id)

        task_type = task.get("task_type")
        if (
            not isinstance(task_type, str)
            or task_type not in _ALLOWED_TASK_TYPES
        ):
            issues.append(
                f"{prefix}.task_type: must be image_to_image or image_to_video"
            )

        source_block_ids = task.get("source_block_ids")
        if not isinstance(source_block_ids, list) or not source_block_ids:
            issues.append(
                f"{prefix}.source_block_ids: must contain at least one block_id"
            )
            source_block_ids = []
        else:
            for block_id in source_block_ids:
                if not isinstance(block_id, str) or block_id not in block_ids:
                    issues.append(
                        f"{prefix}.source_block_ids: unknown block_id {block_id!r}"
                    )
        valid_source_ids = {
            block_id
            for block_id in source_block_ids
            if isinstance(block_id, str) and block_id in block_ids
        }
        task_sources.append(
            (
                display_id,
                task_type if isinstance(task_type, str) else None,
                valid_source_ids,
            )
        )

        references = task.get("reference_images")
        if not isinstance(references, list) or not references:
            issues.append(
                f"{prefix}.reference_images: at least one image is required"
            )
            references = []
        for reference_index, reference in enumerate(references):
            reference_prefix = (
                f"{prefix}.reference_images[{reference_index}]"
            )
            if not isinstance(reference, dict):
                issues.append(f"{reference_prefix}: must be a JSON object")
                continue
            asset_id = reference.get("asset_id")
            if not isinstance(asset_id, str) or not asset_id:
                issues.append(
                    f"{reference_prefix}.asset_id: must be a non-empty string"
                )
                continue
            asset = assets.get(asset_id)
            if asset is None:
                issues.append(
                    f"{reference_prefix}.asset_id: unknown asset_id {asset_id}"
                )
                continue
            if (
                asset.download_error is not None
                or asset.size <= 0
                or not asset.sha256
                or not asset.local_path.is_file()
            ):
                issues.append(
                    f"{reference_prefix}.asset_id: asset {asset_id} download failed"
                )
            if not asset.mime_type.startswith("image/"):
                issues.append(
                    f"{reference_prefix}.asset_id: asset {asset_id} must have image MIME"
                )

        if task_type == "image_to_image":
            if not isinstance(task.get("image_size"), str) or not task.get(
                "image_size"
            ):
                issues.append(
                    f"{prefix}.image_size: required for image_to_image"
                )
            for field_name in ("duration", "resolution", "generate_audio"):
                if task.get(field_name) is not None:
                    issues.append(
                        f"{prefix}.{field_name}: not allowed for image_to_image"
                    )
        elif task_type == "image_to_video":
            if not isinstance(task.get("duration"), int) or isinstance(
                task.get("duration"), bool
            ):
                issues.append(
                    f"{prefix}.duration: required for image_to_video"
                )
            if not isinstance(task.get("resolution"), str) or not task.get(
                "resolution"
            ):
                issues.append(
                    f"{prefix}.resolution: required for image_to_video"
                )
            if task.get("image_size") is not None:
                issues.append(
                    f"{prefix}.image_size: not allowed for image_to_video"
                )
            generate_audio = task.get("generate_audio")
            if generate_audio is not None and not isinstance(
                generate_audio, bool
            ):
                issues.append(
                    f"{prefix}.generate_audio: must be true, false, or omitted"
                )

        output_count = task.get("output_count", 1)
        if (
            not isinstance(output_count, int)
            or isinstance(output_count, bool)
            or output_count < 1
        ):
            issues.append(f"{prefix}.output_count: must be an integer >= 1")
        else:
            total_output_count += output_count

    if total_output_count > max_output_count:
        issues.append(
            "plan.total output_count: "
            f"{total_output_count} exceeds max_output_count {max_output_count}"
        )

    for table_id, required_ids in storyboard_requirements.items():
        relevant_ids = {table_id, *required_ids}
        relevant_tasks = [
            task
            for task in task_sources
            if task[2].intersection(relevant_ids)
        ]
        if len(relevant_tasks) != 1:
            issues.append(
                f"storyboard table {table_id}: exactly one image_to_video task "
                f"must cover all content blocks {required_ids!r}; "
                f"found {len(relevant_tasks)}"
            )
            continue

        task_name, task_type, source_ids = relevant_tasks[0]
        if task_type != "image_to_video":
            issues.append(
                f"storyboard table {table_id}: task {task_name} must be "
                "image_to_video"
            )
        missing_ids = [
            block_id
            for block_id in required_ids
            if block_id not in source_ids
        ]
        if missing_ids:
            issues.append(
                f"storyboard table {table_id}: task {task_name} missing "
                f"source_block_ids {missing_ids!r}"
            )

    return issues


class DeepSeekPlanner:
    def __init__(self, model: Any, *, max_output_count: int = 4) -> None:
        self._model = model.bind(
            response_format={"type": "json_object"},
            extra_body={
                "thinking": {"type": "enabled"},
                "reasoning_effort": "high",
            },
        )
        self.max_output_count = max_output_count

    async def plan(
        self,
        document: NormalizedDocument,
        visions: list[VisionDescription],
        feedback: str | None = None,
    ) -> TaskPlan:
        messages = [
            {"role": "system", "content": _PLAN_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": self._planning_prompt(document, visions, feedback),
            },
        ]

        def validate_payload(payload: dict[str, Any]) -> list[str]:
            return validate_plan(payload, document, self.max_output_count)

        result = await self._invoke_with_repair(
            messages=messages,
            schema=TaskPlan,
            deterministic_validator=validate_payload,
            document_id=document.document_id,
            operation="plan",
        )
        return result

    async def audit(
        self,
        document: NormalizedDocument,
        plan: TaskPlan,
    ) -> AuditReport:
        messages = [
            {"role": "system", "content": _AUDIT_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": self._audit_prompt(document, plan),
            },
        ]
        return await self._invoke_with_repair(
            messages=messages,
            schema=AuditReport,
            deterministic_validator=lambda payload: [],
            document_id=document.document_id,
            operation="audit",
        )

    def _planning_prompt(
        self,
        document: NormalizedDocument,
        visions: list[VisionDescription],
        feedback: str | None,
    ) -> str:
        table_ids = {
            block.block_id
            for block in document.blocks
            if block.block_type == "table"
        }
        table_blocks = [
            block.model_dump(mode="json")
            for block in document.blocks
            if block.block_id in table_ids
            or any(part in table_ids for part in block.path)
        ]
        media_references = [
            {
                "asset_id": asset.asset_id,
                "source_block_id": asset.source_block_id,
                "mime_type": asset.mime_type,
                "width": asset.width,
                "height": asset.height,
                "download_succeeded": (
                    asset.download_error is None
                    and asset.size > 0
                    and bool(asset.sha256)
                    and asset.local_path.is_file()
                ),
            }
            for asset in document.media_assets
        ]
        vision_payload = [
            vision.model_dump(mode="json") for vision in visions
        ]
        schema = TaskPlan.model_json_schema()
        return "\n".join(
            [
                "请把以下文档规划为可执行生成任务。",
                "允许的 task_type 只有 image_to_image 和 image_to_video。",
                (
                    "图片匹配优先级：文档显式引用或同一表格行 > "
                    "同一章节/路径 > 视觉描述语义匹配 > 文档顺序；不得虚构图片。"
                ),
                (
                    "分镜合并规则：同一分镜表的多行必须合并为一个视频任务，"
                    "在一个 prompt 中按镜头顺序描述；不得按镜头拆成多个付费任务。"
                ),
                (
                    "自由叙述按完整意图生成任务；混合图片/视频需求按不同意图"
                    "分别生成对应任务，不要错误合并。"
                ),
                f"max_output_count={self.max_output_count}",
                f"document_id={document.document_id}",
                "稳定 text_view（含 [block:*] / [image:*] 引用）：",
                document.text_view,
                f"序列化表格及后代 blocks={_compact_json(table_blocks)}",
                f"可用图片引用={_compact_json(media_references)}",
                f"全部视觉描述={_compact_json(vision_payload)}",
                f"用户反馈={_compact_json(feedback)}",
                f"TaskPlan JSON Schema={_compact_json(schema)}",
                "只返回符合 Schema 的 JSON 对象。",
            ]
        )

    @staticmethod
    def _audit_prompt(
        document: NormalizedDocument,
        plan: TaskPlan,
    ) -> str:
        return "\n".join(
            [
                "独立审查以下计划，只报告遗漏、冲突、虚构和供应商限制。",
                "不得改写计划，不得返回修正后的 tasks。",
                f"document_id={document.document_id}",
                f"text_view={document.text_view}",
                f"plan={_compact_json(plan.model_dump(mode='json'))}",
                (
                    "AuditReport JSON Schema="
                    f"{_compact_json(AuditReport.model_json_schema())}"
                ),
                "只返回符合 Schema 的 JSON 对象。",
            ]
        )

    async def _invoke_with_repair(
        self,
        *,
        messages: list[dict[str, Any]],
        schema: type[BaseModel],
        deterministic_validator: Callable[[dict[str, Any]], list[str]],
        document_id: str,
        operation: str,
    ) -> Any:
        repair_message: dict[str, str] | None = None
        last_errors: list[str] = []
        for attempt in range(2):
            request_messages = list(messages)
            if repair_message is not None:
                request_messages.append(repair_message)

            model_error: AgentError | None = None
            response: object | None = None
            try:
                response = await self._model.ainvoke(request_messages)
            except Exception as exc:
                model_error = self._model_error(document_id, operation, exc)
            if model_error is not None:
                raise model_error

            raw_content = self._response_content(response)
            parsed, last_errors = self._parse_and_validate(
                raw_content,
                schema,
                deterministic_validator,
            )
            if parsed is not None:
                return parsed

            if attempt == 0:
                repair_message = {
                    "role": "user",
                    "content": self._repair_prompt(raw_content, last_errors),
                }

        raise self._validation_error(
            document_id,
            operation,
            len(last_errors),
        )

    @staticmethod
    def _response_content(response: object | None) -> object:
        if response is None:
            return None
        if isinstance(response, (str, dict)):
            return response
        return getattr(response, "content", None)

    @staticmethod
    def _parse_and_validate(
        raw_content: object,
        schema: type[BaseModel],
        deterministic_validator: Callable[[dict[str, Any]], list[str]],
    ) -> tuple[BaseModel | None, list[str]]:
        if isinstance(raw_content, dict):
            payload: object = raw_content
        elif isinstance(raw_content, str):
            try:
                payload = json.loads(raw_content)
            except (json.JSONDecodeError, TypeError):
                return None, ["response: invalid JSON object"]
        else:
            return None, ["response: missing JSON object"]

        if not isinstance(payload, dict):
            return None, ["response: top-level JSON must be an object"]

        errors = deterministic_validator(payload)
        parsed: BaseModel | None = None
        try:
            parsed = schema.model_validate(payload)
        except ValidationError as exc:
            errors.extend(DeepSeekPlanner._compact_validation_errors(exc))
        if errors:
            return None, errors
        return parsed, []

    @staticmethod
    def _compact_validation_errors(exc: ValidationError) -> list[str]:
        errors = []
        for error in exc.errors(include_url=False, include_input=False):
            location = ".".join(str(part) for part in error["loc"])
            message = str(error["msg"])
            errors.append(f"schema.{location}: {message}"[:240])
        return errors[:12]

    @staticmethod
    def _repair_prompt(raw_content: object, errors: list[str]) -> str:
        if isinstance(raw_content, str):
            raw_text = raw_content
        elif isinstance(raw_content, dict):
            raw_text = _compact_json(raw_content)
        else:
            raw_text = "null"
        concise_errors = "\n".join(f"- {error}" for error in errors[:12])
        return "\n".join(
            [
                "原始输出：",
                raw_text,
                "校验错误：",
                concise_errors,
                "仅返回修复后的 JSON 对象，不要解释或输出推理过程。",
            ]
        )

    @classmethod
    def _model_error(
        cls,
        document_id: str,
        operation: str,
        exc: Exception,
    ) -> AgentError:
        status_code = cls._status_code(exc)
        exception_name = type(exc).__name__
        lowered_name = exception_name.lower()
        technical_detail = (
            f"document_id={document_id}; operation={operation}; "
            f"cause={exception_name}"
        )
        if status_code is not None:
            technical_detail += f"; status={status_code}"
        retryable = (
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
        )
        if retryable:
            return AgentError(
                ErrorDetail(
                    category=ErrorCategory.TRANSIENT,
                    message=(
                        "需求规划模型暂时不可用"
                        f"（document_id={document_id}）"
                    ),
                    technical_detail=technical_detail,
                    retryable=True,
                )
            )
        return AgentError(
            ErrorDetail(
                category=ErrorCategory.PROVIDER_TERMINAL,
                message=f"需求规划模型调用失败（document_id={document_id}）",
                technical_detail=technical_detail,
                retryable=False,
            )
        )

    @staticmethod
    def _validation_error(
        document_id: str,
        operation: str,
        error_count: int,
    ) -> AgentError:
        return AgentError(
            ErrorDetail(
                category=ErrorCategory.VALIDATION,
                message=(
                    "模型两次返回的 JSON 均未通过校验"
                    f"（document_id={document_id}）"
                ),
                technical_detail=(
                    f"document_id={document_id}; operation={operation}; "
                    f"attempts=2; error_count={error_count}"
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
