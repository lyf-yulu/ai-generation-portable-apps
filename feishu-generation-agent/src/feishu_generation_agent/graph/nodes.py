import asyncio
from inspect import Parameter, signature
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any, TypeVar
from uuid import uuid4

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END
from langgraph.types import Command, interrupt
from pydantic import ValidationError

from feishu_generation_agent.config import Settings
from io import BytesIO
from pathlib import Path

from PIL import Image

from feishu_generation_agent.domain.asset_library import normalize_alias
from feishu_generation_agent.domain.character_matcher import match_characters
from feishu_generation_agent.integrations.image_resize import (
    cover_crop,
    parse_size_variant,
)
from feishu_generation_agent.domain.document import (
    MediaAsset,
    NormalizedDocument,
    PlanningPromptSnapshot,
    RequirementRequest,
    VisionDescription,
    IngestIssueSeverity,
    build_planning_prompt_snapshot,
    resolve_ingest_issue_records,
)
from feishu_generation_agent.domain.errors import (
    AgentError,
    ErrorCategory,
    ErrorDetail,
)
from feishu_generation_agent.domain.plan import (
    ApprovalDecision,
    AuditReport,
    GenerationTask,
    TaskPlan,
    TaskType,
    reconcile_task_asset_coverage,
)
from feishu_generation_agent.domain.artifact import (
    Artifact,
    ExecutionRecord,
    ProviderSubmission,
)
from feishu_generation_agent.integrations.planner import (
    image_planner_system_prompt,
    language_validation_message,
    planner_system_prompt,
    validate_plan,
)
from feishu_generation_agent.ports import (
    DeliveryWriter,
    DocumentSource,
    ImageGenerator,
    RequirementPlanner,
    VideoGenerator,
    VisionAnalyzer,
)
from feishu_generation_agent.storage.files import FileStore
from feishu_generation_agent.storage.repository import Repository

from .state import AgentState


@dataclass(frozen=True, slots=True)
class GraphServices:
    document_source: DocumentSource
    vision_analyzer: VisionAnalyzer
    planner: RequirementPlanner
    image_generator: ImageGenerator
    video_generator: VideoGenerator
    delivery_writer: DeliveryWriter
    repository: Repository
    file_store: FileStore
    settings: Settings
    portrait_video_generator: Any | None = None
    production_task_store: Any | None = None
    # 图片 provider registry：{"banana": gen, "seedream": gen, "gpt-image2": gen}。
    # 为 None 时回落到单实例 image_generator，保持存量调用方零改动。
    image_providers: Mapping[str, ImageGenerator] | None = None
    # 角色素材库与语义匹配器，图片模式下用于自动挂载角色参考图。
    # 未配置时跳过自动挂载，人工仍可在审核界面手动挂。
    asset_library_store: Any | None = None
    character_matcher: Any | None = None


_Result = TypeVar("_Result")
_NODE_SUMMARIES = {
    "ingest_source": "Source ingestion",
    "normalize_document": "Document normalization",
    "analyze_images": "Image analysis",
    "plan_requirements": "Requirement planning",
    "audit_plan": "Plan audit",
    "validate_plan": "Plan validation",
    "human_approval": "Human approval",
    "revalidate_approval": "Approval revalidation",
    "check_source_revision": "Source revision check",
    "execute_selected_tasks": "Approved task execution",
    "verify_and_download_artifacts": "Artifact verification",
    "deliver_to_feishu": "Feishu delivery",
}

_PENDING_PROVIDER_STATUSES = frozenset(
    {"submitted", "pending", "queued", "running", "processing"}
)
_SUCCESS_PROVIDER_STATUSES = frozenset({"succeeded", "completed", "success"})
_TERMINAL_PROVIDER_PHASES = frozenset(
    {"submission_uncertain", "failed", "cancelled", "expired", "timed_out"}
)
async_sleep = asyncio.sleep


def _validation_error(message: str = "The request is invalid") -> AgentError:
    return AgentError(
        ErrorDetail(
            category=ErrorCategory.VALIDATION,
            message=message,
            technical_detail="Input validation failed",
            retryable=False,
        )
    )


def _safe_error(exc: BaseException) -> AgentError:
    if isinstance(exc, AgentError):
        category = exc.detail.category
        retryable = exc.detail.retryable
        if category is ErrorCategory.VALIDATION:
            message = (
                exc.detail.message
                if exc.detail.message.startswith(
                    ("模型三次返回的 JSON 均未通过", "以下字段必须包含中文主体说明")
                )
                else "The request is invalid"
            )
        else:
            message = "The workflow node could not be completed"
    elif isinstance(exc, ValidationError):
        category = ErrorCategory.VALIDATION
        retryable = False
        message = "The request is invalid"
    else:
        category = ErrorCategory.TRANSIENT
        retryable = False
        message = "The workflow node could not be completed"
    # 保留原始 technical_detail：planner 会把具体校验失败项写进去，
    # 无条件覆盖成通用文案会让「三次校验未通过」这类故障完全不可诊断，
    # 每次都得另写脚本复现。message 仍走上面的脱敏逻辑。
    inner_detail = (
        exc.detail.technical_detail
        if isinstance(exc, AgentError) and exc.detail.technical_detail
        else None
    )
    technical_detail = f"{category.value} in workflow node"
    if inner_detail:
        technical_detail = f"{technical_detail}; {inner_detail}"
    return AgentError(
        ErrorDetail(
            category=category,
            message=message,
            technical_detail=technical_detail,
            retryable=retryable,
        )
    )


async def _run_node(
    state: AgentState,
    node: str,
    services: GraphServices,
    operation: Callable[[], Awaitable[_Result]],
) -> _Result:
    run_id = state.get("run_id", "unknown-run")
    summary = _NODE_SUMMARIES[node]
    await services.repository.append_event(
        run_id, node, "started", f"{summary} started"
    )
    failure: AgentError | None = None
    try:
        result = await operation()
    except Exception as exc:
        failure = _safe_error(exc)
    if failure is not None:
        await services.repository.append_event(
            run_id,
            node,
            "failed",
            f"{summary} failed ({failure.detail.category.value})",
        )
        raise failure
    await services.repository.append_event(
        run_id, node, "completed", f"{summary} completed"
    )
    return result


def _configured_thread_id(config: Mapping[str, Any]) -> str | None:
    configurable = config.get("configurable")
    if not isinstance(configurable, Mapping):
        return None
    value = configurable.get("thread_id")
    return value if isinstance(value, str) and value else None


def _ensure_thread_id(state: AgentState, config: Mapping[str, Any]) -> None:
    state_thread_id = state.get("thread_id")
    config_thread_id = _configured_thread_id(config)
    if (
        not isinstance(state_thread_id, str)
        or not state_thread_id
        or config_thread_id != state_thread_id
    ):
        raise _validation_error("The workflow thread is invalid")


def _document_for_checkpoint(document: NormalizedDocument) -> NormalizedDocument:
    assets = [
        asset.model_copy(update={"file_token": None})
        for asset in document.media_assets
    ]
    return document.model_copy(update={"media_assets": assets})


def _json_model(model: Any) -> dict[str, Any]:
    payload = model.model_dump(mode="json")
    json.dumps(payload, ensure_ascii=False)
    return payload


def _draft_plan(state: AgentState) -> Any:
    plan = state.get("draft_plan")
    return plan if plan is not None else state.get("task_plan")


def approved_plan_from_state(
    state: AgentState,
    *,
    max_output_count: int,
) -> TaskPlan:
    saved = state.get("approved_plan")
    if isinstance(saved, dict):
        return TaskPlan.model_validate(saved)

    draft = TaskPlan.model_validate(_draft_plan(state))
    approved_tasks = [
        GenerationTask.model_validate(task)
        for task in state.get("approved_tasks", [])
    ]
    selected_ids = [
        task.task_id
        for task in approved_tasks
    ]
    reconciled = reconcile_task_asset_coverage(draft, approved_tasks)
    return reconciled.approved_subset(
        selected_ids,
        max_output_count,
    )


def _document_revision(state: AgentState) -> Any:
    revision = state.get("document_revision")
    return revision if revision is not None else state.get("source_revision")


def _planning_prompt(state: AgentState) -> PlanningPromptSnapshot:
    value = state.get("planning_prompt")
    if value is not None:
        return PlanningPromptSnapshot.model_validate(value)
    # 快照必须按 mode 选契约：直连 run 走 prime-local 分支，快照会通过
    # exact_system_prompt 传给 planner，而该分支排在 image_mode 判断之前，
    # 直接压过图片契约。快照装错内容，图片模板就永远不会被执行。
    prompt_text = (
        image_planner_system_prompt()
        if state.get("planning_mode") == "image"
        else planner_system_prompt()
    )
    return build_planning_prompt_snapshot(
        owner_user_id="prime-local",
        source="prime",
        version=0,
        prompt_text=prompt_text,
    )


_IMAGE_REQUIREMENT_TYPE = "图片类"
_LOGGER = logging.getLogger(__name__)
# 同步型出图 provider：submit 即终态、结果直接落盘，没有异步轮询阶段。
# 下面两类判定原先写死 provider == "chiyun"，registry 拆出 banana /
# gpt-image2 / seedream 后必须一起覆盖，否则它们会绕过标识校验与
# 「落盘失败 → 提交状态未定」的判定。
_SYNC_IMAGE_PROVIDERS = frozenset(
    {"chiyun", "banana", "gpt-image2", "seedream"}
)


async def _resolve_character_assets(
    document: NormalizedDocument,
    services: GraphServices,
) -> list[Any]:
    """把文档里出现的角色对应到素材库条目。

    两级匹配：先按名字/别名字面命中，未覆盖的描述式指代交给语义匹配层。
    任一环节失败都退回已有结果，不让素材库问题拖垮整个 run——挂不上参考图
    只是少了自动化，人工仍可在审核界面手动挂。
    """
    store = getattr(services, "asset_library_store", None)
    if store is None:
        return []
    try:
        library = await store.list_all(limit=500)
    except Exception:
        _LOGGER.warning("素材库读取失败，跳过角色自动挂载", exc_info=True)
        return []
    if not library:
        return []

    by_id = {asset.asset_id: asset for asset in library}
    exact_matches = match_characters(list(document.blocks), library)
    exact_ids = [anchor.asset_id for anchor in exact_matches]
    semantic_ids: list[str] = []

    # 只有单一变体的角色才作为锚点。锚点的含义是「已确定、不要再输出」，
    # 而同名多变体恰恰需要语义层按上下文挑一个，把它们锁成锚点会剥夺
    # 这个判断机会。
    grouped: dict[str, list[Any]] = {}
    for anchor in exact_matches:
        grouped.setdefault(normalize_alias(anchor.name), []).append(anchor)
    anchors = [group[0] for group in grouped.values() if len(group) == 1]

    matcher = getattr(services, "character_matcher", None)
    if matcher is not None:
        try:
            result = await matcher.match(
                document.text_view, library, anchors=anchors
            )
        except Exception:
            _LOGGER.warning("角色语义匹配失败，仅使用精确匹配", exc_info=True)
        else:
            semantic_ids = [item.asset_id for item in result.matches]
            # 文档里出现但库里没有的角色，首次遇到即自动建档并一并挂上。
            for record in await _auto_ingest_characters(
                document, list(result.unresolved_candidates), store
            ):
                by_id[record.asset_id] = record
                semantic_ids.append(record.asset_id)

    return _collapse_variants(exact_ids, semantic_ids, by_id)


def _collapse_variants(
    exact_ids: list[str],
    semantic_ids: list[str],
    by_id: dict[str, Any],
) -> list[Any]:
    """同一角色只保留一个着装变体。

    角色名字面命中时，库里该角色的所有变体都会被精确匹配挑出来。把两套
    冲突服装同时作为参考图会让模型不知道该用哪套——角色一致性正是要避免
    这个。因此按角色名收敛：语义层明确挑过的变体优先，否则取精确匹配的
    第一个。
    """
    chosen: dict[str, str] = {}
    order: list[str] = []
    for asset_id in [*exact_ids, *semantic_ids]:
        asset = by_id.get(asset_id)
        if asset is None:
            continue
        key = normalize_alias(asset.name)
        if key not in chosen:
            chosen[key] = asset_id
            order.append(key)
        elif asset_id in semantic_ids and chosen[key] not in semantic_ids:
            # 语义层的选择覆盖精确匹配的任意变体。
            chosen[key] = asset_id
    return [by_id[chosen[key]] for key in order]


async def _auto_ingest_characters(
    document: NormalizedDocument,
    candidates: list[Any],
    store: Any | None,
) -> list[Any]:
    """把文档里出现、但素材库还没有的角色自动建档。

    参考图取「候选人物所在 block 之后最近的一张图」——需求文档惯例是
    角色名在前、设定图紧随其后。找不到图就跳过，宁可少建也不建错。

    自动入库的条目打 auto-ingested 标签并记录来源文档，方便人工事后复核；
    variant 一律先用「默认」，同人不同着装由人工在素材库界面拆分改名。
    """
    if store is None or not candidates:
        return []

    image_blocks = [
        block
        for block in sorted(document.blocks, key=lambda item: item.order)
        if block.image_asset_id
    ]
    if not image_blocks:
        return []
    assets_by_id = {
        asset.asset_id: asset for asset in document.media_assets
    }
    block_order = {
        block.block_id: block.order for block in document.blocks
    }

    created: list[Any] = []
    for candidate in candidates:
        name = candidate.proposed_name.strip()
        if not name:
            continue
        asset = _nearest_image_asset(
            candidate.block_ids, image_blocks, block_order, assets_by_id
        )
        if asset is None:
            continue
        try:
            content = asset.local_path.read_bytes()
        except OSError:
            _LOGGER.warning("自动入库读取参考图失败 name=%s", name)
            continue
        try:
            record = await store.create(
                name=name,
                variant="默认",
                content=content,
                mime_type=asset.mime_type,
                description=candidate.reason or "",
                tags=["auto-ingested", f"需求文档:{document.document_id}"],
            )
        except Exception:
            # 重名冲突或落盘失败都只跳过该候选，不影响其它候选与整个 run。
            _LOGGER.warning("自动入库失败 name=%s", name, exc_info=True)
            continue
        created.append(record)
    return created


def _nearest_image_asset(
    block_ids: tuple[str, ...],
    image_blocks: list[Any],
    block_order: dict[str, int],
    assets_by_id: dict[str, Any],
) -> Any | None:
    anchors = [
        block_order[block_id]
        for block_id in block_ids
        if block_id in block_order
    ]
    if not anchors:
        return None
    anchor = min(anchors)
    following = [
        block for block in image_blocks if block.order >= anchor
    ]
    chosen = following[0] if following else image_blocks[-1]
    return assets_by_id.get(chosen.image_asset_id)


async def _character_media_assets(
    resolved: list[Any],
    store: Any | None,
) -> list[MediaAsset]:
    """把素材库条目转成 MediaAsset，使其能被下游按普通参考图消费。

    转换后追加进 document.media_assets，_task_assets / provider / 校验
    全部走既有路径，无需为素材库单开分支。
    """
    if store is None or not resolved:
        return []
    media: list[MediaAsset] = []
    for asset in resolved:
        try:
            path = store.local_path(asset)
            content = path.read_bytes()
        except (OSError, AttributeError):
            _LOGGER.warning(
                "素材库文件不可读，跳过挂载 asset_id=%s", asset.asset_id
            )
            continue
        media.append(
            MediaAsset(
                asset_id=asset.asset_id,
                source_block_id=f"asset-library:{asset.asset_id}",
                origin="asset_library",
                local_path=path,
                mime_type=asset.mime_type,
                size=len(content),
                sha256=sha256(content).hexdigest(),
            )
        )
    return media


def _character_context_argument(
    planner: RequirementPlanner,
    resolved: list[Any],
) -> dict[str, str]:
    """把已解析的角色素材描述成 planner 可读的上下文。

    与 _planner_prompt_argument / _planner_mode_argument 同样的签名探测做法：
    老 planner 没有该参数时省略，避免 TypeError。
    """
    if not resolved:
        return {}
    try:
        parameters = signature(planner.plan).parameters
    except (TypeError, ValueError):
        return {}
    parameter = parameters.get("character_context")
    if parameter is None or parameter.kind is Parameter.POSITIONAL_ONLY:
        return {}

    lines = [
        f"- {asset.name} / {asset.variant}（asset_id={asset.asset_id}）"
        f"：{asset.prompt_fragment or asset.description or '无附加描述'}"
        for asset in resolved
    ]
    return {"character_context": "\n".join(lines)}


async def _planning_mode_for_run(
    run_id: str,
    services: GraphServices,
    state: Mapping[str, Any] | None = None,
) -> str:
    """推导规划模式。

    优先级：
    1. state 里的显式声明——直连文档创建的 run 没有多维表格 binding，
       模式在创建时声明；人工改过模式时也以此为准。
    2. binding.planning_mode——图片需求来自另一张表，那张表没有
       「需求类型」字段，无法靠字段值判定。
    3. 需求类型字段——存量 binding 没有 planning_mode 时的回落。

    读取失败一律回落 video，不让一次多维表格抖动把整个 run 拖挂。
    """
    declared_state = (state or {}).get("planning_mode")
    if declared_state in {"image", "video"}:
        return declared_state
    store = getattr(services, "production_task_store", None)
    if store is None:
        return "video"
    try:
        binding = await store.get_by_run(run_id)
    except Exception:
        return "video"
    if binding is None:
        return "video"
    declared = getattr(binding, "planning_mode", None)
    if declared in {"image", "video"}:
        return declared
    task_type = getattr(getattr(binding, "snapshot", None), "task_type", None)
    return "image" if task_type == _IMAGE_REQUIREMENT_TYPE else "video"


def _planner_mode_argument(
    planner: RequirementPlanner,
    mode: str,
) -> dict[str, str]:
    """只在 planner 支持 mode 且需要非默认值时才传。

    存量测试里的 fake planner 大多没有 mode 参数，直接传会 TypeError；
    video 是默认值，省略可进一步减少对存量调用方的干扰。
    """
    if mode == "video":
        return {}
    try:
        parameters = signature(planner.plan).parameters
    except (TypeError, ValueError):
        return {}
    parameter = parameters.get("mode")
    if parameter is None or parameter.kind is Parameter.POSITIONAL_ONLY:
        return {}
    return {"mode": mode}


def _planner_prompt_argument(
    planner: RequirementPlanner,
    planning_prompt: PlanningPromptSnapshot,
) -> dict[str, str | None]:
    try:
        parameters = signature(planner.plan).parameters
    except (TypeError, ValueError):
        parameters = {}
    exact_parameter = parameters.get("exact_system_prompt")
    is_local_prime = (
        planning_prompt.owner_user_id == "prime-local"
        and planning_prompt.source == "prime"
    )
    if (
        is_local_prime
        and exact_parameter is not None
        and exact_parameter.kind is not Parameter.POSITIONAL_ONLY
    ):
        return {"exact_system_prompt": planning_prompt.prompt_text}
    if is_local_prime:
        raise _validation_error(
            "The planner cannot replay the Prime prompt snapshot exactly"
        )
    parameter = parameters.get("system_prompt")
    if parameter is None or parameter.kind is Parameter.POSITIONAL_ONLY:
        raise _validation_error(
            "The planner cannot accept the planning prompt snapshot"
        )
    return {"system_prompt": planning_prompt.prompt_text}


async def ingest_source(
    state: AgentState,
    config: RunnableConfig,
    *,
    services: GraphServices,
) -> AgentState:
    async def operation() -> AgentState:
        _ensure_thread_id(state, config)
        source_url = state.get("source_url")
        if not isinstance(source_url, str) or not source_url:
            raise _validation_error("A source URL is required")
        planning_prompt = _planning_prompt(state)
        request = RequirementRequest(
            source_url=source_url,
            requester_open_id=state.get("requester_open_id"),
            trigger_type=state.get("trigger_type", "local_link"),
            reply_context=state.get("reply_context", {}),
            planning_prompt=planning_prompt,
        )
        document = _document_for_checkpoint(
            await services.document_source.ingest(request)
        )
        document_json = _json_model(document)
        return {
            "status": "running",
            "requirement_request": _json_model(request),
            "planning_prompt": _json_model(planning_prompt),
            "source_document": document_json,
            "source_type": document.source_type.value,
            "source_token": document.source_token,
            "document_id": document.document_id,
            "document_title": document.title,
            "document_revision": document.revision,
            "media_assets": document_json["media_assets"],
            "approval_decision": None,
            "approved_tasks": [],
            "approved_plan": None,
            "execution_records": [],
            "artifacts": [],
            "delivery_record": None,
            "last_error": None,
        }

    return await _run_node(state, "ingest_source", services, operation)


async def normalize_document(
    state: AgentState,
    config: RunnableConfig,
    *,
    services: GraphServices,
) -> AgentState:
    async def operation() -> AgentState:
        _ensure_thread_id(state, config)
        document = NormalizedDocument.model_validate(state.get("source_document"))
        document_json = _json_model(document)
        return {
            "normalized_document": document_json,
            "source_type": document.source_type.value,
            "source_token": document.source_token,
            "document_id": document.document_id,
            "document_title": document.title,
            "document_revision": document.revision,
            "media_assets": document_json["media_assets"],
            "source_revision": document.revision,
        }

    return await _run_node(state, "normalize_document", services, operation)


_VISION_MAX_CONCURRENCY = 5


async def analyze_images(
    state: AgentState,
    config: RunnableConfig,
    *,
    services: GraphServices,
) -> AgentState:
    async def operation() -> AgentState:
        _ensure_thread_id(state, config)
        document = NormalizedDocument.model_validate(
            state.get("normalized_document")
        )
        assets = list(document.media_assets)
        semaphore = asyncio.Semaphore(_VISION_MAX_CONCURRENCY)

        async def analyze_one(asset: MediaAsset) -> VisionDescription | Exception:
            async with semaphore:
                try:
                    return await services.vision_analyzer.analyze(asset)
                except Exception as exc:
                    return exc

        outcomes = await asyncio.gather(
            *(analyze_one(asset) for asset in assets)
        )

        descriptions: list[VisionDescription] = []
        issues: list[str] = []
        for asset, outcome in zip(assets, outcomes):
            if isinstance(outcome, VisionDescription):
                descriptions.append(outcome)
                continue
            reason = (
                outcome.detail.message
                if isinstance(outcome, AgentError)
                else "图片无法完成视觉分析"
            )
            issues.append(
                f"素材 {asset.asset_id} 视觉分析失败：{reason}"
            )
        return {
            "vision_descriptions": [
                _json_model(description) for description in descriptions
            ],
            "vision_issues": issues,
        }

    return await _run_node(state, "analyze_images", services, operation)


async def plan_requirements(
    state: AgentState,
    config: RunnableConfig,
    *,
    services: GraphServices,
) -> AgentState:
    async def operation() -> AgentState:
        _ensure_thread_id(state, config)
        document = NormalizedDocument.model_validate(
            state.get("normalized_document")
        )
        descriptions = [
            VisionDescription.model_validate(item)
            for item in state.get("vision_descriptions", [])
        ]
        planning_prompt = _planning_prompt(state)
        mode = await _planning_mode_for_run(state["run_id"], services, state)
        resolved_characters = (
            await _resolve_character_assets(document, services)
            if mode == "image"
            else []
        )
        plan = await services.planner.plan(
            document,
            descriptions,
            state.get("planner_feedback"),
            **_planner_prompt_argument(services.planner, planning_prompt),
            **_planner_mode_argument(services.planner, mode),
            **_character_context_argument(
                services.planner, resolved_characters
            ),
        )
        plan_json = _json_model(plan)
        updates: AgentState = {
            "draft_plan": plan_json,
            "task_plan": plan_json,
        }
        # 素材库参考图要进 document.media_assets，否则 _task_assets 解析
        # 不到这些 asset_id，执行阶段会直接判定计划无效。
        character_media = await _character_media_assets(
            resolved_characters,
            getattr(services, "asset_library_store", None),
        )
        if character_media:
            known = {asset.asset_id for asset in document.media_assets}
            merged = list(document.media_assets) + [
                asset
                for asset in character_media
                if asset.asset_id not in known
            ]
            updates["normalized_document"] = _json_model(
                document.model_copy(update={"media_assets": merged})
            )
        return updates

    return await _run_node(state, "plan_requirements", services, operation)


async def audit_plan(
    state: AgentState,
    config: RunnableConfig,
    *,
    services: GraphServices,
) -> AgentState:
    async def operation() -> AgentState:
        _ensure_thread_id(state, config)
        document = NormalizedDocument.model_validate(
            state.get("normalized_document")
        )
        plan = TaskPlan.model_validate(_draft_plan(state))
        report = await services.planner.audit(document, plan)
        return {"audit_report": _json_model(report)}

    return await _run_node(state, "audit_plan", services, operation)


async def validate_planned_tasks(
    state: AgentState,
    config: RunnableConfig,
    *,
    services: GraphServices,
) -> AgentState:
    async def operation() -> AgentState:
        _ensure_thread_id(state, config)
        plan = TaskPlan.model_validate(_draft_plan(state))
        document = NormalizedDocument.model_validate(
            state.get("normalized_document")
        )
        issues = [
            record.display_message
            for record in resolve_ingest_issue_records(document)
            if record.severity is IngestIssueSeverity.BLOCKING
        ]
        issues.extend(
            validate_plan(
                plan,
                document,
                max_output_count=services.settings.max_output_count,
            )
        )
        audit = AuditReport.model_validate(state.get("audit_report", {}))
        if audit.corrections_required:
            issues.extend(f"audit: {issue}" for issue in audit.issues)
        return {"validation_issues": issues, "status": "waiting_approval"}

    return await _run_node(state, "validate_plan", services, operation)


def _approval_payload(state: AgentState) -> dict[str, Any]:
    plan = _draft_plan(state)
    revision = _document_revision(state)
    payload = {
        "action": "review_plan",
        "run_id": state.get("run_id"),
        "thread_id": state.get("thread_id"),
        "status": "waiting_approval",
        "document_revision": revision,
        "source_revision": revision,
        "draft_plan": plan,
        "task_plan": plan,
        "audit_report": state.get("audit_report"),
        "validation_issues": state.get("validation_issues", []),
    }
    json.dumps(payload, ensure_ascii=False)
    return payload


def _parse_approval(value: Any) -> ApprovalDecision:
    if not isinstance(value, dict):
        raise _validation_error()
    allowed_keys = {"action", "selected_task_ids", "tasks", "feedback"}
    if set(value) - allowed_keys:
        raise _validation_error()
    try:
        decision = ApprovalDecision.model_validate(value)
    except Exception:
        decision = None
    if decision is None:
        raise _validation_error()

    if decision.action == "reject":
        if (
            not isinstance(decision.feedback, str)
            or not decision.feedback.strip()
            or decision.selected_task_ids
            or decision.tasks
        ):
            raise _validation_error()
    elif decision.action == "cancel":
        if (
            decision.selected_task_ids
            or decision.tasks
            or decision.feedback is not None
        ):
            raise _validation_error()
    elif (
        not decision.selected_task_ids
        or decision.feedback is not None
        or len(decision.selected_task_ids)
        != len(set(decision.selected_task_ids))
    ):
        raise _validation_error()
    return decision


async def human_approval(
    state: AgentState,
    config: RunnableConfig,
    *,
    services: GraphServices,
) -> Command:
    _ensure_thread_id(state, config)
    resume_value = interrupt(_approval_payload(state))

    async def operation() -> Command:
        decision = _parse_approval(resume_value)
        decision_json = _json_model(decision)
        if decision.action == "reject":
            return Command(
                update={
                    "approval_decision": decision_json,
                    "planner_feedback": decision.feedback.strip(),
                    "approved_tasks": [],
                    "approved_plan": None,
                    "status": "running",
                },
                goto="plan_requirements",
            )
        if decision.action == "cancel":
            return Command(
                update={
                    "approval_decision": decision_json,
                    "approved_tasks": [],
                    "approved_plan": None,
                    "status": "cancelled",
                },
                goto=END,
            )

        original = TaskPlan.model_validate(_draft_plan(state))
        candidate = (
            reconcile_task_asset_coverage(original, decision.tasks)
            if decision.tasks
            else original
        )
        original_ids = {task.task_id for task in original.tasks}
        if any(task.task_id not in original_ids for task in candidate.tasks):
            raise _validation_error()
        try:
            approved = candidate.approved_subset(
                decision.selected_task_ids,
                services.settings.max_output_count,
            )
        except Exception:
            approved = None
        if approved is None or not approved.tasks:
            raise _validation_error()
        return Command(
            update={
                "approval_decision": decision_json,
                "approval_revision": _document_revision(state),
                "approved_tasks": [
                    _json_model(task) for task in approved.tasks
                ],
                "approved_plan": _json_model(approved),
                "status": "approval_pending_validation",
            },
            goto="revalidate_approval",
        )

    return await _run_node(state, "human_approval", services, operation)


async def revalidate_approval(
    state: AgentState,
    config: RunnableConfig,
    *,
    services: GraphServices,
) -> AgentState:
    async def operation() -> AgentState:
        _ensure_thread_id(state, config)
        approval_revision = state.get("approval_revision")
        if (
            not isinstance(approval_revision, int)
            or isinstance(approval_revision, bool)
            or approval_revision < 0
            or approval_revision != _document_revision(state)
        ):
            raise _validation_error()
        draft = TaskPlan.model_validate(_draft_plan(state))
        decision = ApprovalDecision.model_validate(
            state.get("approval_decision")
        )
        if decision.action != "approve":
            raise _validation_error()
        candidate = (
            reconcile_task_asset_coverage(draft, decision.tasks)
            if decision.tasks
            else draft
        )
        draft_ids = {task.task_id for task in draft.tasks}
        if any(task.task_id not in draft_ids for task in candidate.tasks):
            raise _validation_error()
        selected_plan = candidate.approved_subset(
            decision.selected_task_ids,
            services.settings.max_output_count,
        )
        checkpoint_plan = approved_plan_from_state(
            state,
            max_output_count=services.settings.max_output_count,
        )
        if checkpoint_plan.model_dump(mode="json") != selected_plan.model_dump(
            mode="json"
        ):
            raise _validation_error()
        document = NormalizedDocument.model_validate(
            state.get("normalized_document")
        )
        issues = [
            record.display_message
            for record in resolve_ingest_issue_records(document)
            if record.severity is IngestIssueSeverity.BLOCKING
        ]
        issues.extend(
            validate_plan(
                selected_plan,
                document,
                max_output_count=services.settings.max_output_count,
            )
        )
        if issues:
            raise _validation_error(
                language_validation_message(issues)
                or "The approved plan is not valid"
            )
        return {"validation_issues": [], "status": "approved"}

    return await _run_node(
        state, "revalidate_approval", services, operation
    )


async def check_source_revision(
    state: AgentState,
    config: RunnableConfig,
    *,
    services: GraphServices,
) -> Command:
    async def operation() -> Command:
        _ensure_thread_id(state, config)
        source_url = state.get("source_url")
        approval_revision = state.get("approval_revision")
        if (
            not isinstance(source_url, str)
            or not source_url
            or not isinstance(approval_revision, int)
            or isinstance(approval_revision, bool)
            or approval_revision < 0
        ):
            raise _validation_error()
        current_revision = await services.document_source.get_revision(source_url)
        if current_revision != approval_revision:
            await services.repository.append_event(
                state.get("run_id", "unknown-run"),
                "check_source_revision",
                "source_changed",
                "Source revision changed; approval cleared",
            )
            return Command(
                update={
                    "approval_decision": None,
                    "approval_revision": None,
                    "approved_tasks": [],
                    "approved_plan": None,
                    "status": "running",
                },
                goto="ingest_source",
            )
        return Command(
            update={"status": "approved"}, goto="execute_selected_tasks"
        )

    return await _run_node(
        state, "check_source_revision", services, operation
    )


def _execution_error(exc: BaseException) -> dict[str, object]:
    safe = _safe_error(exc).detail
    result: dict[str, object] = {
        "category": safe.category.value,
        "message": _safe_execution_message(safe.category),
        "retryable": safe.retryable,
    }
    if isinstance(exc, AgentError):
        code = _safe_execution_code(exc.detail.technical_detail)
        if code is not None:
            result["code"] = code
    return result


def _safe_execution_message(category: ErrorCategory) -> str:
    return {
        ErrorCategory.CONFIGURATION: "生成服务配置不完整",
        ErrorCategory.PERMISSION: "生成服务凭证无效或权限不足",
        ErrorCategory.DOCUMENT: "参考素材无法用于生成",
        ErrorCategory.VALIDATION: "生成参数不符合供应商要求",
        ErrorCategory.TRANSIENT: "生成服务暂时不可用，请稍后重试",
        ErrorCategory.PROVIDER_TERMINAL: "生成服务拒绝了请求",
        ErrorCategory.DELIVERY: "生成结果交付失败",
    }[category]


def _safe_execution_code(technical_detail: str) -> str | None:
    fields: dict[str, str] = {}
    for part in technical_detail.split(";"):
        key, separator, value = part.strip().partition("=")
        normalized = value.strip().lower().replace("-", "_")
        if (
            separator
            and key.strip() in {
                "operation",
                "status",
                "cause",
                "provider_code",
            }
            and normalized
            and normalized.replace("_", "").replace(".", "").isalnum()
            and len(normalized) <= 48
        ):
            fields[key.strip()] = normalized
    operation = fields.get("operation", "generation")
    if "provider_code" in fields:
        return f"{operation}_{fields['provider_code']}"
    if "status" in fields:
        return f"{operation}_http_{fields['status']}"
    if "cause" in fields:
        return f"{operation}_{fields['cause']}"
    return None


_OUTPUT_SLOT_MARKER = "::output:"


def _execution_units(task: GenerationTask) -> list[GenerationTask]:
    # 图片与视频都按 output_count 拆分执行单元；早期只有视频支持多产出，
    # 图片模式接入后这个类型门槛不再成立。
    if task.output_count == 1:
        return [task]
    return [
        task.model_copy(
            update={
                "task_id": f"{task.task_id}{_OUTPUT_SLOT_MARKER}{index}",
                "output_count": 1,
            }
        )
        for index in range(1, task.output_count + 1)
    ]


def _provider_terminal_error(message: str) -> AgentError:
    return AgentError(
        ErrorDetail(
            category=ErrorCategory.PROVIDER_TERMINAL,
            message=message,
            technical_detail="Provider execution returned an invalid terminal result",
            retryable=False,
        )
    )


def _validate_submission_identity(
    submission: ProviderSubmission,
    *,
    provider: str,
    official_id: str,
) -> None:
    if (
        submission.provider != provider
        or submission.provider_task_id != official_id
    ):
        raise _provider_terminal_error("供应商任务身份不一致")


def _task_assets(
    task: GenerationTask,
    document: NormalizedDocument,
) -> list[MediaAsset]:
    assets_by_id = {asset.asset_id: asset for asset in document.media_assets}
    ordered = sorted(task.reference_images, key=lambda reference: reference.order)
    if [reference.order for reference in ordered] != list(
        range(1, len(ordered) + 1)
    ):
        raise _validation_error("The approved plan is not valid")
    try:
        return [assets_by_id[reference.asset_id] for reference in ordered]
    except KeyError:
        raise _validation_error("The approved plan is not valid") from None


def _task_fingerprint(task: GenerationTask) -> str:
    canonical = json.dumps(
        task.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def _intent_is_stale(
    operation: dict[str, Any], lease_seconds: float
) -> bool:
    try:
        updated_at = datetime.fromisoformat(operation["updated_at"])
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=UTC)
    except (KeyError, TypeError, ValueError):
        return True
    return (datetime.now(UTC) - updated_at).total_seconds() >= (
        lease_seconds
    )


async def _keep_submission_intent_alive(
    services: GraphServices,
    run_id: str,
    task: GenerationTask,
    provider: str,
    client_id: str,
    task_fingerprint: str,
) -> None:
    interval = max(
        0.01, min(5.0, services.settings.submission_intent_lease_seconds / 3)
    )
    while True:
        await async_sleep(interval)
        renewed = await services.repository.renew_submission_intent_lease(
            run_id, task.task_id, client_id, provider, task_fingerprint
        )
        if not renewed:
            return


async def _generator_for_task(run_id: str, task: GenerationTask, services: GraphServices):
    if task.task_type is TaskType.IMAGE_TO_IMAGE:
        registry = getattr(services, "image_providers", None)
        if not registry:
            # 未配置 registry：沿用单实例与历史 provider 名，存量 run 不受影响。
            return "chiyun", services.image_generator
        requested = task.resolved_image_provider
        generator = registry.get(requested)
        if generator is None:
            raise _validation_error(
                f"图片 provider {requested} 未配置，"
                f"当前可用：{'、'.join(sorted(registry))}"
            )
        return requested, generator
    if services.portrait_video_generator is not None and services.production_task_store is not None:
        binding = await services.production_task_store.get_by_run(run_id)
        if binding is not None and binding.snapshot.task_type == "真人类":
            return "volcengine_portrait", services.portrait_video_generator.for_run(run_id)
    return "seedance", services.video_generator


async def _transition_operation(
    services: GraphServices,
    run_id: str,
    task_id: str,
    operation: dict[str, Any],
    phase: str,
    official_id: str | None,
) -> bool:
    client_id = operation.get("client_submission_id")
    if not isinstance(client_id, str):
        return False
    return await services.repository.compare_and_set_operation(
        run_id,
        task_id,
        operation["operation"],
        expected_phase=operation["phase"],
        expected_client_submission_id=client_id,
        expected_official_id=operation.get("official_id"),
        expected_provider=operation["provider"],
        expected_task_fingerprint=operation["task_fingerprint"],
        phase=phase,
        official_id=official_id,
    )


async def _existing_valid_artifacts(
    services: GraphServices,
    run_id: str,
    task: GenerationTask,
) -> list[Artifact] | None:
    artifacts = await services.repository.list_artifacts(
        run_id, task_id=task.task_id
    )
    if len(artifacts) != task.output_count:
        if artifacts:
            await services.repository.delete_task_artifacts(run_id, task.task_id)
        return None
    expected_ids = {
        sha256(f"{run_id}\0{task.task_id}\0{index}".encode()).hexdigest()[:32]
        for index in range(task.output_count)
    }
    if {artifact.artifact_id for artifact in artifacts} != expected_ids:
        await services.repository.delete_task_artifacts(run_id, task.task_id)
        return None
    if not all(
        services.file_store.verify_artifact(run_id, artifact)
        for artifact in artifacts
    ):
        await services.repository.delete_task_artifacts(run_id, task.task_id)
        return None
    return artifacts


async def _poll_submission(
    generator: Any,
    submission: ProviderSubmission,
    services: GraphServices,
) -> ProviderSubmission | None:
    current = submission
    for attempt in range(services.settings.provider_poll_max_attempts):
        try:
            current = await generator.poll(current)
        except AgentError as exc:
            if not exc.detail.retryable:
                raise
        else:
            _validate_submission_identity(
                current,
                provider=submission.provider,
                official_id=submission.provider_task_id,
            )
            status = current.status.lower()
            if status not in _PENDING_PROVIDER_STATUSES:
                return current
        if attempt + 1 < services.settings.provider_poll_max_attempts:
            await async_sleep(services.settings.provider_poll_interval_seconds)
    return None


def _resize_image_bytes(content: bytes, variant: str | None) -> bytes | None:
    """把出图字节裁到需求要求的尺寸；无需处理时返回 None。

    必须在落盘前处理：FileStore.verify_artifact 要求产物文件名恰好是
    {sha256}.{extension}，落盘后另写文件会破坏这个契约，导致产物校验
    失败、任务被判 failed。

    裁切失败一律返回 None 走原图——出原图比整个 run 失败好，人工在审核
    界面能看到实际尺寸。
    """
    if not variant:
        return None
    try:
        target = parse_size_variant(variant)
    except ValueError:
        _LOGGER.warning("尺寸变体无法解析，保留原图 variant=%s", variant)
        return None
    try:
        with Image.open(BytesIO(content)) as image:
            if (image.width, image.height) == (target.width, target.height):
                return None
            resized = cover_crop(image.convert("RGB"), target)
            buffer = BytesIO()
            resized.save(buffer, format="PNG")
    except (OSError, ValueError):
        _LOGGER.warning("出图裁切失败，保留原图 variant=%s", variant)
        return None
    return buffer.getvalue()


async def _materialize_submission(
    services: GraphServices,
    run_id: str,
    task: GenerationTask,
    submission: ProviderSubmission,
) -> list[Artifact]:
    if len(submission.result_items) != task.output_count:
        raise _provider_terminal_error("供应商返回的结果数量不正确")
    kind = "image" if task.task_type is TaskType.IMAGE_TO_IMAGE else "video"
    artifacts: list[Artifact] = []
    for index, result in enumerate(submission.result_items):
        # 模型原生出图尺寸与需求要求的尺寸通常不同，落盘前裁一次才是
        # 可交付产物；落盘后再改会破坏 {sha256}.{ext} 的产物命名契约。
        variant = (
            next(iter(task.resolved_size_variants), None)
            if kind == "image"
            else None
        )
        materialized = await services.file_store.materialize_provider_result(
            run_id,
            task.task_id,
            submission.provider_task_id,
            index,
            result,
            kind=kind,
            transform=(
                (lambda content: _resize_image_bytes(content, variant))
                if variant
                else None
            ),
        )
        stored = materialized.stored
        artifact = Artifact(
            artifact_id=sha256(
                f"{run_id}\0{task.task_id}\0{index}".encode()
            ).hexdigest()[:32],
            task_id=task.task_id,
            kind=kind,
            local_path=stored.local_path,
            mime_type=stored.mime_type,
            size=stored.size,
            sha256=stored.sha256,
            provider_url=materialized.provider_url,
            provider_task_id=submission.provider_task_id,
            status="ready",
        )
        artifacts.append(artifact)
    await services.repository.replace_task_artifacts(
        run_id, task.task_id, artifacts
    )
    return artifacts


async def _repair_succeeded_submission(
    services: GraphServices,
    run_id: str,
    task: GenerationTask,
    provider: str,
    generator: Any,
    submit_operation: dict[str, Any],
) -> tuple[ExecutionRecord, list[Artifact]]:
    official_id = submit_operation.get("official_id")
    if not isinstance(official_id, str):
        return ExecutionRecord(
            task_id=task.task_id, provider=provider, status="submission_uncertain"
        ), []
    created, repair = await services.repository.create_artifact_repair_intent_if_absent(
        run_id, task.task_id, provider, official_id, uuid4().hex,
        submit_operation["task_fingerprint"],
    )
    if not created:
        existing = await _existing_valid_artifacts(services, run_id, task)
        if repair["phase"] == "succeeded" and existing is not None:
            return ExecutionRecord(
                task_id=task.task_id, provider=provider,
                provider_task_id=official_id, status="succeeded"
            ), existing
        if repair["phase"] != "intent_created":
            return ExecutionRecord(
                task_id=task.task_id, provider=provider,
                provider_task_id=official_id,
                status=repair["phase"],
            ), []
    try:
        submission = await _poll_submission(
            generator,
            ProviderSubmission(provider=provider, provider_task_id=official_id,
                               status="submitted"),
            services,
        )
        if submission is None:
            target = "timed_out"
            artifacts: list[Artifact] = []
        elif submission.status.lower() not in _SUCCESS_PROVIDER_STATUSES:
            target = "failed"
            artifacts = []
        else:
            artifacts = await _materialize_submission(
                services, run_id, task, submission
            )
            target = "succeeded"
        changed = await _transition_operation(
            services, run_id, task.task_id, repair, target, official_id
        )
        latest = await services.repository.get_operation(
            run_id, task.task_id, repair["operation"]
        )
        authoritative = target if changed else (
            latest["phase"] if latest is not None else "failed"
        )
        if authoritative == "succeeded":
            valid = await _existing_valid_artifacts(services, run_id, task)
            if valid is not None:
                artifacts = valid
            else:
                authoritative = "failed"
                artifacts = []
        elif artifacts:
            await services.repository.delete_task_artifacts(
                run_id, task.task_id
            )
            artifacts = []
        return ExecutionRecord(
            task_id=task.task_id, provider=provider,
            provider_task_id=official_id, status=authoritative,
        ), artifacts
    except Exception as exc:
        latest = await services.repository.get_operation(
            run_id, task.task_id, repair["operation"]
        )
        if latest is not None and latest["phase"] == "intent_created":
            await _transition_operation(
                services, run_id, task.task_id, latest, "failed", official_id
            )
            latest = await services.repository.get_operation(
                run_id, task.task_id, repair["operation"]
            )
        if latest is not None and latest["phase"] == "succeeded":
            valid = await _existing_valid_artifacts(services, run_id, task)
            if valid is not None:
                return ExecutionRecord(
                    task_id=task.task_id, provider=provider,
                    provider_task_id=official_id, status="succeeded",
                ), valid
        return ExecutionRecord(
            task_id=task.task_id, provider=provider,
            provider_task_id=official_id,
            status=latest["phase"] if latest is not None else "failed",
            error=_execution_error(exc),
        ), []


async def _finish_submit_phase(
    services: GraphServices,
    run_id: str,
    task: GenerationTask,
    provider: str,
    operation: dict[str, Any],
    target: str,
    official_id: str,
    artifacts: list[Artifact] | None = None,
    error: dict[str, object] | None = None,
) -> tuple[ExecutionRecord, list[Artifact]]:
    changed = await _transition_operation(
        services, run_id, task.task_id, operation, target, official_id
    )
    latest = await services.repository.get_operation(
        run_id, task.task_id, "submit"
    )
    if latest is None or (
        latest.get("provider") != provider
        or latest.get("task_fingerprint") != _task_fingerprint(task)
        or latest.get("official_id") != official_id
    ):
        return ExecutionRecord(
            task_id=task.task_id, provider=provider,
            provider_task_id=official_id, status="submission_uncertain",
        ), []
    authoritative = target if changed else latest["phase"]
    if authoritative == "succeeded":
        valid = await _existing_valid_artifacts(services, run_id, task)
        if valid is None:
            return ExecutionRecord(
                task_id=task.task_id, provider=provider,
                provider_task_id=official_id, status="failed",
            ), []
        return ExecutionRecord(
            task_id=task.task_id, provider=provider,
            provider_task_id=official_id, status="succeeded",
        ), valid
    if artifacts:
        await services.repository.delete_task_artifacts(run_id, task.task_id)
    return ExecutionRecord(
        task_id=task.task_id, provider=provider,
        provider_task_id=official_id, status=authoritative,
        error=error,
    ), []


async def _execute_one_task(
    services: GraphServices,
    run_id: str,
    task: GenerationTask,
    assets: list[MediaAsset],
) -> tuple[ExecutionRecord, list[Artifact]]:
    task_fingerprint = _task_fingerprint(task)
    provider, generator = await _generator_for_task(run_id, task, services)
    existing_artifacts = await _existing_valid_artifacts(
        services, run_id, task
    )
    operation = await services.repository.get_operation(
        run_id, task.task_id, "submit"
    )
    if operation is not None and (
        operation.get("provider") != provider
        or operation.get("task_fingerprint") != task_fingerprint
    ):
        return (
            ExecutionRecord(
                task_id=task.task_id,
                provider=provider,
                provider_task_id=operation.get("official_id"),
                status="submission_uncertain",
                error={"message": "已保存任务身份与当前审批任务不一致"},
            ),
            [],
        )
    if existing_artifacts is not None:
        if operation is None:
            await services.repository.delete_task_artifacts(run_id, task.task_id)
            existing_artifacts = None
        elif operation["phase"] not in {"submitted", "succeeded"}:
            await services.repository.delete_task_artifacts(run_id, task.task_id)
            return ExecutionRecord(
                task_id=task.task_id, provider=provider,
                provider_task_id=operation.get("official_id"),
                status=operation["phase"],
            ), []
    if existing_artifacts is not None:
        official_id = operation.get("official_id") if operation else None
        if operation is not None and operation["phase"] != "succeeded":
            if not isinstance(official_id, str):
                return ExecutionRecord(
                    task_id=task.task_id, provider=provider,
                    status="submission_uncertain",
                ), []
            return await _finish_submit_phase(
                services, run_id, task, provider, operation, "succeeded",
                official_id, existing_artifacts,
            )
        return (
            ExecutionRecord(
                task_id=task.task_id,
                provider=provider,
                provider_task_id=official_id,
                status="succeeded",
            ),
            existing_artifacts,
        )

    owns_submit = False
    immediate: ProviderSubmission | None = None
    if operation is None:
        created, operation = (
            await services.repository.create_submission_intent_if_absent(
                run_id, task.task_id, provider, uuid4().hex, task_fingerprint
            )
        )
        owns_submit = created

    phase = operation["phase"]
    if phase == "intent_created" and not owns_submit:
        if _intent_is_stale(
            operation, services.settings.submission_intent_lease_seconds
        ):
            client_id = operation.get("client_submission_id")
            if isinstance(client_id, str):
                cutoff = datetime.now(UTC) - timedelta(
                    seconds=services.settings.submission_intent_lease_seconds
                )
                await services.repository.expire_submission_intent_lease(
                    run_id, task.task_id, client_id, provider,
                    task_fingerprint, cutoff.isoformat(),
                )
            latest = await services.repository.get_operation(
                run_id, task.task_id, "submit"
            )
            phase = (latest or {}).get("phase", "submission_uncertain")
        return (
            ExecutionRecord(
                task_id=task.task_id,
                provider=provider,
                status=phase,
            ),
            [],
        )
    if phase in _TERMINAL_PROVIDER_PHASES:
        return (
            ExecutionRecord(
                task_id=task.task_id,
                provider=provider,
                provider_task_id=operation.get("official_id"),
                status=phase,
            ),
            [],
        )

    if phase == "succeeded":
        return await _repair_succeeded_submission(
            services, run_id, task, provider, generator, operation
        )

    if owns_submit:
        client_id = operation["client_submission_id"]
        await services.repository.renew_submission_intent_lease(
            run_id, task.task_id, client_id, provider, task_fingerprint
        )
        heartbeat = asyncio.create_task(
            _keep_submission_intent_alive(
                services, run_id, task, provider, client_id, task_fingerprint
            )
        )
        try:
            immediate = await generator.submit(
                task, assets, submission_id=client_id
            )
            official_id = immediate.provider_task_id
            if immediate.provider != provider:
                raise _provider_terminal_error("供应商任务身份不一致")
            if provider in _SYNC_IMAGE_PROVIDERS and official_id != client_id:
                raise _provider_terminal_error("供应商任务标识不一致")
            transitioned = await _transition_operation(
                services,
                run_id,
                task.task_id,
                operation,
                "submitted",
                official_id,
            )
            if not transitioned:
                latest = await services.repository.get_operation(
                    run_id, task.task_id, "submit"
                )
                if (
                    latest is None
                    or latest["phase"] != "submitted"
                    or latest.get("client_submission_id") != client_id
                    or latest.get("official_id") != official_id
                    or latest.get("provider") != provider
                    or latest.get("task_fingerprint") != task_fingerprint
                ):
                    raise _provider_terminal_error("提交状态无法安全落库")
            operation = await services.repository.get_operation(
                run_id, task.task_id, "submit"
            )
            if operation is None or operation["phase"] != "submitted":
                raise _provider_terminal_error("提交状态无法安全落库")
        except Exception as exc:
            latest = await services.repository.get_operation(
                run_id, task.task_id, "submit"
            )
            local_preflight_failure = (
                isinstance(exc, AgentError)
                and exc.detail.category
                in {ErrorCategory.VALIDATION, ErrorCategory.DOCUMENT}
            )
            if latest is not None and latest["phase"] == "intent_created":
                await _transition_operation(
                    services,
                    run_id,
                    task.task_id,
                    latest,
                    "failed" if local_preflight_failure else "submission_uncertain",
                    None,
                )
                latest = await services.repository.get_operation(
                    run_id, task.task_id, "submit"
                )
            if latest is not None and latest["phase"] == "succeeded":
                return await _repair_succeeded_submission(
                    services, run_id, task, provider, generator, latest
                )
            return (
                ExecutionRecord(
                    task_id=task.task_id,
                    provider=provider,
                    provider_task_id=(latest or {}).get("official_id"),
                    status=(latest or {}).get("phase", "submission_uncertain"),
                    error=_execution_error(exc),
                ),
                [],
            )
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    official_id = operation.get("official_id")
    if not isinstance(official_id, str) or not official_id:
        return (
            ExecutionRecord(
                task_id=task.task_id,
                provider=provider,
                status="submission_uncertain",
            ),
            [],
        )
    submission = immediate
    try:
        if submission is None or submission.status.lower() in _PENDING_PROVIDER_STATUSES:
            submission = await _poll_submission(
                generator,
                ProviderSubmission(
                    provider=provider,
                    provider_task_id=official_id,
                    status="submitted",
                ),
                services,
            )
        if submission is None:
            return await _finish_submit_phase(
                services, run_id, task, provider, operation,
                "timed_out", official_id,
            )
        status = submission.status.lower()
        if status not in _SUCCESS_PROVIDER_STATUSES:
            phase = status if status in {"cancelled", "expired"} else "failed"
            return await _finish_submit_phase(
                services, run_id, task, provider, operation, phase,
                official_id,
                error=_execution_error(
                    _provider_terminal_error("供应商生成任务失败")
                ),
            )
        artifacts = await _materialize_submission(
            services, run_id, task, submission
        )
        latest = await services.repository.get_operation(
            run_id, task.task_id, "submit"
        )
        if latest is None:
            return ExecutionRecord(
                task_id=task.task_id, provider=provider,
                provider_task_id=official_id, status="submission_uncertain",
            ), []
        return await _finish_submit_phase(
            services, run_id, task, provider, latest, "succeeded",
            official_id, artifacts,
        )
    except Exception as exc:
        latest = await services.repository.get_operation(
            run_id, task.task_id, "submit"
        )
        chiyun_staging_invalid = (
            provider in _SYNC_IMAGE_PROVIDERS
            and isinstance(exc, AgentError)
            and exc.detail.category is ErrorCategory.PROVIDER_TERMINAL
            and (
                "operation=materialize" in exc.detail.technical_detail
                or "operation=poll; cause=staging_invalid"
                in exc.detail.technical_detail
            )
        )
        failure_phase = (
            "submission_uncertain" if chiyun_staging_invalid else "failed"
        )
        if latest is not None and latest["phase"] == "submitted":
            return await _finish_submit_phase(
                services, run_id, task, provider, latest, failure_phase,
                official_id, error=_execution_error(exc),
            )
        if latest is not None and latest["phase"] == "succeeded":
            return await _repair_succeeded_submission(
                services, run_id, task, provider, generator, latest
            )
        return (
            ExecutionRecord(
                task_id=task.task_id,
                provider=provider,
                provider_task_id=official_id,
                status=(latest or {}).get("phase", failure_phase),
                error=_execution_error(exc),
            ),
            [],
        )


async def execute_selected_tasks(
    state: AgentState,
    config: RunnableConfig,
    *,
    services: GraphServices,
) -> AgentState:
    async def operation() -> AgentState:
        _ensure_thread_id(state, config)
        run_id = state.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise _validation_error()
        document = NormalizedDocument.model_validate(
            state.get("normalized_document")
        )
        plan = approved_plan_from_state(
            state,
            max_output_count=services.settings.max_output_count,
        )
        issues = [
            record.display_message
            for record in resolve_ingest_issue_records(document)
            if record.severity is IngestIssueSeverity.BLOCKING
        ]
        issues.extend(
            validate_plan(
                plan,
                document,
                max_output_count=services.settings.max_output_count,
            )
        )
        if issues:
            raise _validation_error(
                language_validation_message(issues)
                or "The approved plan is not valid"
            )
        records: list[ExecutionRecord] = []
        artifacts: list[Artifact] = []
        for task in plan.tasks:
            assets = _task_assets(task, document)
            unit_results = await asyncio.gather(
                *(
                    _execute_one_task(services, run_id, unit, assets)
                    for unit in _execution_units(task)
                )
            )
            for record, task_artifacts in unit_results:
                records.append(record)
                artifacts.extend(task_artifacts)
        all_succeeded = all(record.status == "succeeded" for record in records)
        status = (
            "verification_pending"
            if all_succeeded
            else "completed_with_errors"
            if artifacts
            else "failed"
        )
        return {
            "execution_records": [_json_model(record) for record in records],
            "artifacts": [_json_model(artifact) for artifact in artifacts],
            "status": status,
        }

    return await _run_node(
        state, "execute_selected_tasks", services, operation
    )


async def verify_and_download_artifacts(
    state: AgentState,
    config: RunnableConfig,
    *,
    services: GraphServices,
) -> AgentState:
    async def operation() -> AgentState:
        _ensure_thread_id(state, config)
        run_id = state.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise _validation_error()
        artifacts = [
            Artifact.model_validate(item) for item in state.get("artifacts", [])
        ]
        records = [
            ExecutionRecord.model_validate(item)
            for item in state.get("execution_records", [])
        ]
        plan_tasks = TaskPlan(
            tasks=state.get("approved_tasks", []), document_summary=""
        ).tasks
        tasks = {
            unit.task_id: unit
            for task in plan_tasks
            for unit in _execution_units(task)
        }
        record_ids = [record.task_id for record in records]
        if len(record_ids) != len(set(record_ids)) or set(record_ids) != set(tasks):
            raise _provider_terminal_error("生成产物记录不一致")
        successful = {record.task_id: record for record in records
                      if record.status == "succeeded"}
        if any(artifact.task_id not in successful for artifact in artifacts):
            raise _provider_terminal_error("生成产物记录不一致")
        verified: list[Artifact] = []
        for task_id, record in successful.items():
            task = tasks.get(task_id)
            if task is None:
                raise _provider_terminal_error("生成产物记录不一致")
            state_items = sorted(
                (item for item in artifacts if item.task_id == task_id),
                key=lambda item: item.artifact_id,
            )
            repository_items = sorted(
                await services.repository.list_artifacts(run_id, task_id=task_id),
                key=lambda item: item.artifact_id,
            )
            if (
                len(state_items) != task.output_count
                or [item.model_dump(mode="json") for item in state_items]
                != [item.model_dump(mode="json") for item in repository_items]
                or any(item.provider_task_id != record.provider_task_id
                       for item in state_items)
                or not all(services.file_store.verify_artifact(run_id, item)
                           for item in state_items)
            ):
                raise _provider_terminal_error("生成产物记录或文件校验失败")
            verified.extend(state_items)
        all_succeeded = len(successful) == len(records)
        return {
            "artifacts": [_json_model(artifact) for artifact in verified],
            "status": (
                "succeeded"
                if all_succeeded
                else "completed_with_errors"
                if verified
                else "failed"
            ),
        }

    return await _run_node(
        state, "verify_and_download_artifacts", services, operation
    )


async def deliver_to_feishu(
    state: AgentState,
    config: RunnableConfig,
    *,
    services: GraphServices,
) -> AgentState:
    _ensure_thread_id(state, config)
    run_id = state.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise _validation_error()
    summary = _NODE_SUMMARIES["deliver_to_feishu"]
    await services.repository.append_event(
        run_id, "deliver_to_feishu", "started", f"{summary} started"
    )
    try:
        document = NormalizedDocument.model_validate(
            state.get("normalized_document")
        )
        plan = approved_plan_from_state(
            state,
            max_output_count=services.settings.max_output_count,
        )
        artifacts = [
            Artifact.model_validate(item) for item in state.get("artifacts", [])
        ]
        record = await services.delivery_writer.deliver(
            run_id, document, plan, artifacts
        )
    except Exception as exc:
        failure = _safe_error(exc)
        await services.repository.append_event(
            run_id,
            "deliver_to_feishu",
            "failed",
            f"{summary} failed ({failure.detail.category.value})",
        )
        return {
            "status": "delivery_failed",
            "delivery_record": None,
            "last_error": _json_model(failure.detail),
        }
    await services.repository.append_event(
        run_id, "deliver_to_feishu", "completed", f"{summary} completed"
    )
    return {
        "delivery_record": _json_model(record),
        "status": "succeeded" if artifacts else "failed",
        "last_error": None,
    }
