import asyncio
import json
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
from feishu_generation_agent.domain.document import (
    MediaAsset,
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
    GenerationTask,
    TaskPlan,
    TaskType,
)
from feishu_generation_agent.domain.artifact import (
    Artifact,
    ExecutionRecord,
    ProviderSubmission,
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


def _draft_plan(state: AgentState) -> Any:
    plan = state.get("draft_plan")
    return plan if plan is not None else state.get("task_plan")


def _document_revision(state: AgentState) -> Any:
    revision = state.get("document_revision")
    return revision if revision is not None else state.get("source_revision")


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
        document_json = _json_model(document)
        return {
            "status": "running",
            "requirement_request": _json_model(request),
            "source_document": document_json,
            "source_type": document.source_type.value,
            "source_token": document.source_token,
            "document_id": document.document_id,
            "document_title": document.title,
            "document_revision": document.revision,
            "media_assets": document_json["media_assets"],
            "approval_decision": None,
            "approved_tasks": [],
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
        plan_json = _json_model(plan)
        return {"draft_plan": plan_json, "task_plan": plan_json}

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

        original = TaskPlan.model_validate(_draft_plan(state))
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
                "approval_revision": _document_revision(state),
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
            TaskPlan(
                tasks=decision.tasks,
                document_summary=draft.document_summary,
            )
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
        checkpoint_plan = TaskPlan(
            tasks=state.get("approved_tasks", []),
            document_summary=draft.document_summary,
        )
        if checkpoint_plan.model_dump(mode="json") != selected_plan.model_dump(
            mode="json"
        ):
            raise _validation_error()
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
    return {
        "category": safe.category.value,
        "message": safe.message,
        "retryable": safe.retryable,
    }


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


def _provider_for_task(task: GenerationTask) -> str:
    return "chiyun" if task.task_type is TaskType.IMAGE_TO_IMAGE else "seedance"


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


def _generator_for_task(task: GenerationTask, services: GraphServices):
    if task.task_type is TaskType.IMAGE_TO_IMAGE:
        return services.image_generator
    return services.video_generator


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
        materialized = await services.file_store.materialize_provider_result(
            run_id,
            task.task_id,
            submission.provider_task_id,
            index,
            result,
            kind=kind,
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
    provider = _provider_for_task(task)
    task_fingerprint = _task_fingerprint(task)
    generator = _generator_for_task(task, services)
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
            if provider == "chiyun" and official_id != client_id:
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
            provider == "chiyun"
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
        plan = TaskPlan(
            tasks=state.get("approved_tasks", []),
            document_summary=TaskPlan.model_validate(
                _draft_plan(state)
            ).document_summary,
        )
        issues = validate_plan(
            plan,
            document,
            max_output_count=services.settings.max_output_count,
        )
        if issues:
            raise _validation_error("The approved plan is not valid")
        task_assets = [(task, _task_assets(task, document)) for task in plan.tasks]

        records: list[ExecutionRecord] = []
        artifacts: list[Artifact] = []
        for task, assets in task_assets:
            record, task_artifacts = await _execute_one_task(
                services, run_id, task, assets
            )
            records.append(record)
            artifacts.extend(task_artifacts)
        all_succeeded = all(record.status == "succeeded" for record in records)
        return {
            "execution_records": [_json_model(record) for record in records],
            "artifacts": [_json_model(artifact) for artifact in artifacts],
            "status": "verification_pending" if all_succeeded else "completed_with_errors",
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
        tasks = {
            task.task_id: task
            for task in TaskPlan(
                tasks=state.get("approved_tasks", []), document_summary=""
            ).tasks
        }
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
        status = state.get("status")
        return {
            "artifacts": [_json_model(artifact) for artifact in verified],
            "status": "succeeded"
            if status == "verification_pending"
            else "completed_with_errors",
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
        draft = TaskPlan.model_validate(_draft_plan(state))
        plan = TaskPlan(
            tasks=state.get("approved_tasks", []),
            document_summary=draft.document_summary,
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
        "status": "succeeded" if artifacts else "completed_with_errors",
        "last_error": None,
    }
