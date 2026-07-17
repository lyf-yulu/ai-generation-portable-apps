import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, TypeVar

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END
from langgraph.types import Command, interrupt
from pydantic import ValidationError

from feishu_generation_agent.config import Settings
from feishu_generation_agent.domain.document import (
    NormalizedDocument,
    RequirementRequest,
    VisionDescription,
)
from feishu_generation_agent.domain.errors import (
    AgentError,
    ErrorCategory,
    ErrorDetail,
)
from feishu_generation_agent.domain.plan import (
    ApprovalDecision,
    AuditReport,
    TaskPlan,
)
from feishu_generation_agent.integrations.planner import validate_plan
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
}


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
            message = "The request is invalid"
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
    return AgentError(
        ErrorDetail(
            category=category,
            message=message,
            technical_detail=f"{category.value} in workflow node",
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
        request = RequirementRequest(
            source_url=source_url,
            requester_open_id=state.get("requester_open_id"),
            trigger_type=state.get("trigger_type", "local_link"),
            reply_context=state.get("reply_context", {}),
        )
        document = _document_for_checkpoint(
            await services.document_source.ingest(request)
        )
        return {
            "status": "running",
            "requirement_request": _json_model(request),
            "source_document": _json_model(document),
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
        return {
            "normalized_document": _json_model(document),
            "source_revision": document.revision,
        }

    return await _run_node(state, "normalize_document", services, operation)


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
        descriptions = [
            await services.vision_analyzer.analyze(asset)
            for asset in document.media_assets
        ]
        return {
            "vision_descriptions": [
                _json_model(description) for description in descriptions
            ],
            "vision_issues": [],
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
        plan = await services.planner.plan(
            document,
            descriptions,
            state.get("planner_feedback"),
        )
        return {"task_plan": _json_model(plan)}

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
        plan = TaskPlan.model_validate(state.get("task_plan"))
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
        plan = TaskPlan.model_validate(state.get("task_plan"))
        document = NormalizedDocument.model_validate(
            state.get("normalized_document")
        )
        issues = list(state.get("vision_issues", []))
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
    payload = {
        "action": "review_plan",
        "run_id": state.get("run_id"),
        "thread_id": state.get("thread_id"),
        "status": "waiting_approval",
        "source_revision": state.get("source_revision"),
        "task_plan": state.get("task_plan"),
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
                    "status": "running",
                },
                goto="plan_requirements",
            )
        if decision.action == "cancel":
            return Command(
                update={
                    "approval_decision": decision_json,
                    "approved_tasks": [],
                    "status": "cancelled",
                },
                goto=END,
            )

        original = TaskPlan.model_validate(state.get("task_plan"))
        candidate = (
            TaskPlan(
                tasks=decision.tasks,
                document_summary=original.document_summary,
            )
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
                "approval_revision": state.get("source_revision"),
                "approved_tasks": [
                    _json_model(task) for task in approved.tasks
                ],
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
        source_url = state.get("source_url")
        if not isinstance(source_url, str):
            raise _validation_error()
        current_revision = await services.document_source.get_revision(source_url)
        if current_revision != state.get("approval_revision"):
            raise _validation_error("The source changed after planning")
        selected_plan = TaskPlan(
            tasks=state.get("approved_tasks", []),
            document_summary=TaskPlan.model_validate(
                state.get("task_plan")
            ).document_summary,
        )
        document = NormalizedDocument.model_validate(
            state.get("normalized_document")
        )
        issues = list(state.get("vision_issues", []))
        issues.extend(
            validate_plan(
                selected_plan,
                document,
                max_output_count=services.settings.max_output_count,
            )
        )
        audit = AuditReport.model_validate(state.get("audit_report", {}))
        if audit.corrections_required:
            issues.extend(f"audit: {issue}" for issue in audit.issues)
        if issues:
            raise _validation_error("The approved plan is not valid")
        return {"validation_issues": [], "status": "approved"}

    return await _run_node(
        state, "revalidate_approval", services, operation
    )
