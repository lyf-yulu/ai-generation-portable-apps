import hashlib
import json
from dataclasses import dataclass, fields, replace
from types import SimpleNamespace
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.types import Command
from pydantic import ValidationError

from feishu_generation_agent.domain.errors import (
    AgentError,
    ErrorCategory,
    ErrorDetail,
)
from feishu_generation_agent.domain.document import (
    DocumentBlock,
    MediaAsset,
    PlanningPromptSnapshot,
    VisionDescription,
)
from feishu_generation_agent.domain.plan import AuditReport, TaskPlan
from feishu_generation_agent.graph.builder import build_graph
from feishu_generation_agent.graph.nodes import (
    GraphServices,
    analyze_images,
    approved_plan_from_state,
)
from feishu_generation_agent.graph.state import AgentState
from feishu_generation_agent.integrations.planner import DeepSeekPlanner
from feishu_generation_agent.storage.checkpoints import open_checkpointer


_FORMAL_STATE_KEYS = {
    "run_id",
    "thread_id",
    "source_url",
    "planning_prompt",
    "source_type",
    "source_token",
    "document_id",
    "document_title",
    "document_revision",
    "normalized_document",
    "media_assets",
    "vision_descriptions",
    "draft_plan",
    "audit_report",
    "validation_issues",
    "approval_decision",
    "approved_tasks",
    "approved_plan",
    "execution_records",
    "artifacts",
    "delivery_record",
    "status",
    "last_error",
}


@dataclass
class _UnsafeSerdeProbe:
    value: str


class _ResumeOnlyDocumentSource:
    def __init__(self, revision: int) -> None:
        self.revision = revision
        self.ingest_calls = 0
        self.revision_calls = 0

    async def ingest(self, request: Any) -> Any:
        del request
        self.ingest_calls += 1
        raise AssertionError("resume must not ingest again")

    async def get_revision(self, source_url: str) -> int:
        assert source_url.startswith("https://")
        self.revision_calls += 1
        return self.revision


class _NeverCalledVisionAnalyzer:
    def __init__(self) -> None:
        self.calls = 0

    async def analyze(self, asset: Any) -> Any:
        del asset
        self.calls += 1
        raise AssertionError("resume must not analyze images again")


class _PartiallyFailingVisionAnalyzer:
    def __init__(self, failed_asset_id: str) -> None:
        self.failed_asset_id = failed_asset_id
        self.calls: list[str] = []

    async def analyze(self, asset: Any) -> VisionDescription:
        self.calls.append(asset.asset_id)
        if asset.asset_id == self.failed_asset_id:
            raise AgentError(
                ErrorDetail(
                    category=ErrorCategory.DOCUMENT,
                    message="该图片无法识别",
                    technical_detail="fictional-secret-image-detail",
                    retryable=False,
                )
            )
        return VisionDescription(
            asset_id=asset.asset_id,
            subjects=["虚构纸船"],
            scene="虚构河面",
            style="插画",
            composition="居中",
            characters=[],
            actions=[],
            visible_text=[],
            colors=["蓝色"],
            probable_role="参考图",
            uncertainties=[],
        )


class _NeverCalledPlanner:
    def __init__(self) -> None:
        self.plan_calls = 0
        self.audit_calls = 0

    async def plan(self, *args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        self.plan_calls += 1
        raise AssertionError("resume must not plan again")

    async def audit(self, *args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        self.audit_calls += 1
        raise AssertionError("resume must not audit again")


class _LegacySystemPromptPlanner:
    def __init__(self, delegate: Any) -> None:
        self.delegate = delegate
        self.plan_calls = 0

    async def plan(
        self,
        document: Any,
        descriptions: list[Any],
        feedback: str | None,
        system_prompt: str | None = None,
    ) -> Any:
        self.plan_calls += 1
        return await self.delegate.plan(
            document,
            descriptions,
            feedback,
            system_prompt=system_prompt,
        )

    async def audit(self, document: Any, plan: Any) -> Any:
        return await self.delegate.audit(document, plan)


class _NoSystemPromptPlanner:
    def __init__(self, delegate: Any) -> None:
        self.delegate = delegate
        self.plan_calls = 0

    async def plan(
        self,
        document: Any,
        descriptions: list[Any],
        feedback: str | None,
    ) -> Any:
        self.plan_calls += 1
        return await self.delegate.plan(document, descriptions, feedback)

    async def audit(self, document: Any, plan: Any) -> Any:
        return await self.delegate.audit(document, plan)


class _FailingDeliveryWriter:
    def __init__(self) -> None:
        self.deliver_calls = 0

    async def deliver(self, run_id, document, plan, artifacts):
        del run_id, document, plan, artifacts
        self.deliver_calls += 1
        raise RuntimeError("fictional delivery outage")


class _StructuredOutputModel:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls = 0

    def bind(self, **kwargs: Any) -> "_StructuredOutputModel":
        del kwargs
        return self

    async def ainvoke(
        self,
        messages: list[dict[str, Any]],
        config: dict[str, Any] | None = None,
    ) -> SimpleNamespace:
        del messages, config
        self.calls += 1
        return SimpleNamespace(content=self.responses.pop(0))


class _PortraitGeneratorProbe:
    def __init__(self) -> None:
        self.submit_calls = 0
        self.poll_calls = 0

    def for_run(self, run_id: str) -> "_PortraitGeneratorProbe":
        del run_id
        return self


def _input(
    run_id: str,
    thread_id: str,
    planning_prompt: PlanningPromptSnapshot | None = None,
) -> AgentState:
    state: AgentState = {
        "run_id": run_id,
        "thread_id": thread_id,
        "source_url": "https://fiction.feishu.cn/docx/doc-graph",
        "status": "created",
    }
    if planning_prompt is not None:
        state["planning_prompt"] = planning_prompt.model_dump(mode="json")
    return state


def _config(thread_id: str) -> dict[str, dict[str, str]]:
    return {"configurable": {"thread_id": thread_id}}


def _interrupt_payload(result: dict[str, Any]) -> dict[str, Any]:
    return result["__interrupt__"][0].value


def _assert_no_paid_side_effects(services: GraphServices) -> None:
    assert services.image_generator.submit_calls == 0
    assert services.image_generator.poll_calls == 0
    assert services.video_generator.submit_calls == 0
    assert services.video_generator.poll_calls == 0
    if services.portrait_video_generator is not None:
        assert services.portrait_video_generator.submit_calls == 0
        assert services.portrait_video_generator.poll_calls == 0
    assert services.delivery_writer.deliver_calls == 0


async def test_image_analysis_keeps_other_assets_and_reports_single_failure(
    fake_services: GraphServices,
) -> None:
    document = fake_services.document_source.document
    first = document.media_assets[0].model_copy(
        update={"asset_id": "sheet-failed"}
    )
    second = document.media_assets[0].model_copy(
        update={"asset_id": "sheet-success"}
    )
    document = document.model_copy(update={"media_assets": [first, second]})
    analyzer = _PartiallyFailingVisionAnalyzer("sheet-failed")
    services = replace(fake_services, vision_analyzer=analyzer)
    state: AgentState = {
        "run_id": "run-partial-vision",
        "thread_id": "thread-partial-vision",
        "normalized_document": document.model_dump(mode="json"),
    }

    result = await analyze_images(
        state,
        _config("thread-partial-vision"),
        services=services,
    )

    assert analyzer.calls == ["sheet-failed", "sheet-success"]
    assert [
        item["asset_id"] for item in result["vision_descriptions"]
    ] == ["sheet-success"]
    assert len(result["vision_issues"]) == 1
    assert "sheet-failed" in result["vision_issues"][0]
    assert "该图片无法识别" in result["vision_issues"][0]
    assert "fictional-secret-image-detail" not in result["vision_issues"][0]


def test_agent_state_and_graph_services_contracts_are_stable():
    assert AgentState.__total__ is False
    assert _FORMAL_STATE_KEYS <= set(AgentState.__annotations__)
    assert {
        "run_id",
        "thread_id",
        "source_url",
        "planning_prompt",
        "status",
        "requester_open_id",
        "trigger_type",
        "reply_context",
        "requirement_request",
        "source_document",
        "normalized_document",
        "source_revision",
        "vision_descriptions",
        "vision_issues",
        "task_plan",
        "audit_report",
        "validation_issues",
        "planner_feedback",
        "approval_decision",
        "approval_revision",
        "approved_tasks",
        "execution_records",
        "artifacts",
        "delivery_record",
        "error",
    } <= set(AgentState.__annotations__)
    assert [field.name for field in fields(GraphServices)] == [
        "document_source",
        "vision_analyzer",
        "planner",
        "image_generator",
        "video_generator",
        "delivery_writer",
            "repository",
            "file_store",
            "settings",
            "portrait_video_generator",
            "production_task_store",
        ]


def test_planning_prompt_snapshot_rejects_text_hash_mismatch() -> None:
    with pytest.raises(ValueError):
        PlanningPromptSnapshot(
            owner_user_id="user-a",
            source="personal",
            version=2,
            prompt_text="个人版本 v2",
            prompt_sha256="0" * 64,
        )


def test_planning_prompt_snapshot_is_immutable_and_copy_updates_are_validated() -> None:
    prompt_text = "个人版本 v2"
    snapshot = PlanningPromptSnapshot(
        owner_user_id="user-a",
        source="personal",
        version=2,
        prompt_text=prompt_text,
        prompt_sha256=hashlib.sha256(prompt_text.encode("utf-8")).hexdigest(),
    )

    with pytest.raises(ValidationError):
        snapshot.prompt_text = "被意外修改"
    with pytest.raises(ValidationError, match="prompt_sha256"):
        snapshot.model_copy(update={"prompt_text": "绕过校验的修改"})


async def test_personal_prompt_snapshot_is_checkpointed_and_reused_for_replan(
    fake_services: GraphServices,
):
    prompt_text = "个人版本 v2：保持角色造型一致"
    planning_prompt = PlanningPromptSnapshot(
        owner_user_id="user-a",
        source="personal",
        version=2,
        prompt_text=prompt_text,
        prompt_sha256=hashlib.sha256(prompt_text.encode("utf-8")).hexdigest(),
    )
    graph = build_graph(fake_services, InMemorySaver())
    config = _config("thread-personal-v2")

    await graph.ainvoke(
        _input("run-personal-v2", "thread-personal-v2", planning_prompt),
        config=config,
    )
    snapshot = await graph.aget_state(config)
    result = await graph.ainvoke(
        Command(resume={"action": "reject", "feedback": "改成暖色"}),
        config=config,
    )

    assert snapshot.values["planning_prompt"] == planning_prompt.model_dump(
        mode="json"
    )
    assert result["planning_prompt"] == planning_prompt.model_dump(mode="json")
    assert fake_services.planner.system_prompts == [prompt_text, prompt_text]


async def test_direct_graph_run_snapshots_exact_prime_prompt(
    fake_services: GraphServices,
):
    graph = build_graph(fake_services, InMemorySaver())
    config = _config("thread-prime-snapshot")

    await graph.ainvoke(
        _input("run-prime-snapshot", "thread-prime-snapshot"),
        config=config,
    )
    snapshot = await graph.aget_state(config)
    planning_prompt = PlanningPromptSnapshot.model_validate(
        snapshot.values["planning_prompt"]
    )

    assert planning_prompt.owner_user_id == "prime-local"
    assert planning_prompt.source == "prime"
    assert planning_prompt.version == 0
    assert planning_prompt.prompt_sha256 == (
        "fc009b4bb8351502a9412b88a5554a8567a9aa9a633eba588fb673b513f16db1"
    )
    assert fake_services.planner.system_prompts == [planning_prompt.prompt_text]


async def test_local_prime_snapshot_is_used_verbatim_for_initial_and_replan(
    fake_services: GraphServices,
):
    historical_prompt = "历史 Prime 快照：这一版本必须原样复用"
    planning_prompt = PlanningPromptSnapshot(
        owner_user_id="prime-local",
        source="prime",
        version=0,
        prompt_text=historical_prompt,
        prompt_sha256=hashlib.sha256(
            historical_prompt.encode("utf-8")
        ).hexdigest(),
    )
    graph = build_graph(fake_services, InMemorySaver())
    config = _config("thread-prime-history")

    await graph.ainvoke(
        _input("run-prime-history", "thread-prime-history", planning_prompt),
        config=config,
    )
    await graph.ainvoke(
        Command(resume={"action": "reject", "feedback": "重新规划"}),
        config=config,
    )

    assert fake_services.planner.system_prompts == [
        historical_prompt,
        historical_prompt,
    ]


async def test_graph_fails_closed_on_tampered_prompt_snapshot(
    fake_services: GraphServices,
):
    prompt_text = "可信的历史快照"
    planning_prompt = PlanningPromptSnapshot(
        owner_user_id="prime-local",
        source="prime",
        version=0,
        prompt_text=prompt_text,
        prompt_sha256=hashlib.sha256(prompt_text.encode("utf-8")).hexdigest(),
    )
    state = _input("run-tampered-prompt", "thread-tampered-prompt", planning_prompt)
    state["planning_prompt"]["prompt_text"] = "篡改后文本"
    graph = build_graph(fake_services, InMemorySaver())

    with pytest.raises(AgentError):
        await graph.ainvoke(state, config=_config("thread-tampered-prompt"))

    assert fake_services.planner.plan_calls == 0


async def test_local_prime_fails_closed_for_legacy_planner_without_exact_replay(
    fake_services: GraphServices,
):
    legacy_planner = _LegacySystemPromptPlanner(fake_services.planner)
    services = replace(fake_services, planner=legacy_planner)
    graph = build_graph(services, InMemorySaver())

    with pytest.raises(AgentError) as raised:
        await graph.ainvoke(
            _input("run-legacy-planner", "thread-legacy-planner"),
            config=_config("thread-legacy-planner"),
        )

    assert raised.value.detail.category == ErrorCategory.VALIDATION
    assert legacy_planner.plan_calls == 0


@pytest.mark.parametrize(
    ("source", "version", "prompt_text"),
    [
        pytest.param(
            "personal",
            2,
            "个人 v2：保持角色一致",
            id="personal-v2",
        ),
        pytest.param(
            "prime",
            0,
            "Portal 用户当前使用 Prime",
            id="portal-prime",
        ),
    ],
)
async def test_portal_prompt_fails_closed_when_planner_cannot_accept_snapshot(
    fake_services: GraphServices,
    source: str,
    version: int,
    prompt_text: str,
):
    planning_prompt = PlanningPromptSnapshot(
        owner_user_id="portal-user-a",
        source=source,
        version=version,
        prompt_text=prompt_text,
        prompt_sha256=hashlib.sha256(prompt_text.encode("utf-8")).hexdigest(),
    )
    no_prompt_planner = _NoSystemPromptPlanner(fake_services.planner)
    services = replace(fake_services, planner=no_prompt_planner)
    graph = build_graph(services, InMemorySaver())

    with pytest.raises(AgentError) as raised:
        await graph.ainvoke(
            _input(
                f"run-no-prompt-{source}",
                f"thread-no-prompt-{source}",
                planning_prompt,
            ),
            config=_config(f"thread-no-prompt-{source}"),
        )

    assert raised.value.detail.category == ErrorCategory.VALIDATION
    assert no_prompt_planner.plan_calls == 0


async def test_graph_pauses_before_any_generation(fake_services: GraphServices):
    graph = build_graph(fake_services, InMemorySaver())
    config = _config("thread-1")

    result = await graph.ainvoke(_input("run-1", "thread-1"), config=config)

    payload = _interrupt_payload(result)
    assert payload["action"] == "review_plan"
    assert payload["run_id"] == "run-1"
    assert payload["thread_id"] == "thread-1"
    assert payload["status"] == "waiting_approval"
    assert payload["document_revision"] == 7
    assert payload["draft_plan"]["tasks"][0]["task_id"] == "task-video"
    assert payload["task_plan"]["tasks"][0]["task_id"] == "task-video"
    assert payload["audit_report"] == {
        "issues": [],
        "corrections_required": False,
    }
    assert payload["validation_issues"] == []
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "fictional-graph-image-bytes" not in serialized
    assert "must-not-persist" not in serialized
    assert "base64" not in serialized.lower()
    _assert_no_paid_side_effects(fake_services)
    assert await fake_services.repository.count_operations() == 0


async def test_blocking_ingest_issue_is_visible_and_blocks_edited_approval(
    fake_services: GraphServices,
):
    issue = "阻塞：内嵌电子表格 NuBUx5 读取失败（Block fiction-sheet）"
    document = fake_services.document_source.document
    fake_services.document_source.document = document.model_copy(
        update={"ingest_issues": [issue]}
    )
    graph = build_graph(fake_services, InMemorySaver())
    config = _config("thread-blocking-ingest")

    first = await graph.ainvoke(
        _input("run-blocking-ingest", "thread-blocking-ingest"),
        config=config,
    )

    payload = _interrupt_payload(first)
    assert payload["validation_issues"] == [
        "飞书电子表格读取失败，请重新读取后再审批"
    ]
    edited_tasks = payload["draft_plan"]["tasks"]
    edited_tasks[0]["prompt"] += "，保持构图不变"
    with pytest.raises(AgentError) as raised:
        await graph.ainvoke(
            Command(
                resume={
                    "action": "approve",
                    "selected_task_ids": ["task-video"],
                    "tasks": edited_tasks,
                }
            ),
            config=config,
        )

    assert raised.value.detail.category == ErrorCategory.VALIDATION
    _assert_no_paid_side_effects(fake_services)
    assert await fake_services.repository.count_operations() == 0


async def test_single_asset_and_vision_failures_do_not_block_other_asset(
    fake_services: GraphServices,
):
    document = fake_services.document_source.document
    failed = document.media_assets[0].model_copy(
        update={
            "asset_id": "sheet-failed",
            "source_block_id": "fiction-sheet",
            "local_path": document.media_assets[0].local_path.parent
            / "sheet-failed.missing",
            "size": 0,
            "sha256": "",
            "download_error": "虚构图片保存失败",
        }
    )
    fake_services.document_source.document = document.model_copy(
        update={
            "media_assets": [document.media_assets[0], failed],
            "ingest_issues": [
                "素材失败：内嵌电子表格素材 sheet-failed 保存失败"
            ],
        }
    )
    services = replace(
        fake_services,
        vision_analyzer=_PartiallyFailingVisionAnalyzer("sheet-failed"),
    )
    graph = build_graph(services, InMemorySaver())
    config = _config("thread-nonblocking-ingest")

    first = await graph.ainvoke(
        _input("run-nonblocking-ingest", "thread-nonblocking-ingest"),
        config=config,
    )

    payload = _interrupt_payload(first)
    assert payload["validation_issues"] == []
    result = await graph.ainvoke(
        Command(
            resume={
                "action": "approve",
                "selected_task_ids": ["task-video"],
                "tasks": payload["draft_plan"]["tasks"],
            }
        ),
        config=config,
    )

    assert result["status"] == "succeeded"
    assert services.video_generator.submit_calls == 1
    assert services.delivery_writer.deliver_calls == 1


async def test_three_english_only_plans_fail_before_any_paid_generator_call(
    fake_services: GraphServices,
):
    invalid_plan = {
        "tasks": [
            fake_services.planner.task.model_dump(mode="json") | {
                "user_intent": "Generate a paper boat video",
                "prompt": "A paper boat drifts into the distance",
            }
        ],
        "document_summary": "Generate a paper boat video",
    }
    model = _StructuredOutputModel(
        [json.dumps(invalid_plan), json.dumps(invalid_plan), json.dumps(invalid_plan)]
    )
    services = replace(
        fake_services,
        planner=DeepSeekPlanner(model, max_output_count=4),
        portrait_video_generator=_PortraitGeneratorProbe(),
    )
    graph = build_graph(services, InMemorySaver())

    with pytest.raises(AgentError) as raised:
        await graph.ainvoke(
            _input("run-english-only", "thread-english-only"),
            config=_config("thread-english-only"),
        )

    assert model.calls == 3
    assert raised.value.detail.category == ErrorCategory.VALIDATION
    assert "中文" in raised.value.detail.message
    assert "plan.document_summary" in raised.value.detail.message
    assert "tasks[0].user_intent" in raised.value.detail.message
    assert "Generate a paper boat video" not in raised.value.detail.message
    _assert_no_paid_side_effects(services)
    assert await services.repository.count_operations() == 0


async def test_approval_edit_with_english_only_prompt_fails_before_generator(
    fake_services: GraphServices,
):
    graph = build_graph(fake_services, InMemorySaver())
    config = _config("thread-english-approval-edit")
    first = await graph.ainvoke(
        _input("run-english-approval-edit", "thread-english-approval-edit"),
        config=config,
    )
    plan = _interrupt_payload(first)["task_plan"]
    edited_task = plan["tasks"][0] | {
        "user_intent": "Generate a paper boat video",
        "prompt": "A paper boat drifts into the distance",
    }

    with pytest.raises(AgentError) as raised:
        await graph.ainvoke(
            Command(
                resume={
                    "action": "approve",
                    "selected_task_ids": ["task-video"],
                    "tasks": [edited_task],
                }
            ),
            config=config,
        )

    assert raised.value.detail.category == ErrorCategory.VALIDATION
    assert "tasks[0].user_intent" in raised.value.detail.message
    assert "tasks[0].prompt" in raised.value.detail.message
    assert "Generate a paper boat video" not in raised.value.detail.message
    _assert_no_paid_side_effects(fake_services)
    assert await fake_services.repository.count_operations() == 0


async def test_checkpointed_state_is_plain_json_and_nodes_record_safe_events(
    fake_services: GraphServices,
):
    graph = build_graph(fake_services, InMemorySaver())
    config = _config("thread-json")

    await graph.ainvoke(_input("run-json", "thread-json"), config=config)
    snapshot = await graph.aget_state(config)

    assert _FORMAL_STATE_KEYS <= set(snapshot.values)
    serialized_state = json.dumps(snapshot.values, ensure_ascii=False)
    assert snapshot.values["status"] == "waiting_approval"
    assert snapshot.values["source_type"] == "docx"
    assert snapshot.values["source_token"] == "doc-graph"
    assert snapshot.values["document_id"] == "doc-graph"
    assert snapshot.values["document_title"] == "纸船审批测试"
    assert snapshot.values["document_revision"] == 7
    assert snapshot.values["draft_plan"] == snapshot.values["task_plan"]
    assert snapshot.values["approval_decision"] is None
    assert snapshot.values["approved_tasks"] == []
    assert snapshot.values["execution_records"] == []
    assert snapshot.values["artifacts"] == []
    assert snapshot.values["delivery_record"] is None
    assert snapshot.values["last_error"] is None
    assert snapshot.values["media_assets"][0]["file_token"] is None
    assert snapshot.values["source_document"]["media_assets"][0][
        "file_token"
    ] is None
    assert "fictional-file-token" not in serialized_state
    assert "must-not-persist" not in serialized_state
    assert "base64" not in serialized_state.lower()
    events = await fake_services.repository.list_events("run-json")
    completed_nodes = [
        "ingest_source",
        "normalize_document",
        "analyze_images",
        "plan_requirements",
        "audit_plan",
        "validate_plan",
    ]
    assert [(event["node"], event["status"]) for event in events] == [
        pair
        for node in completed_nodes
        for pair in ((node, "started"), (node, "completed"))
    ]
    summaries = " ".join(event["summary"] for event in events)
    assert "must-not-persist" not in summaries
    assert "fictional-graph-image-bytes" not in summaries
    assert "[block:story-1]" not in summaries


async def test_reject_with_feedback_replans_and_interrupts_again(
    fake_services: GraphServices,
):
    graph = build_graph(fake_services, InMemorySaver())
    config = _config("thread-reject")
    await graph.ainvoke(_input("run-reject", "thread-reject"), config=config)

    result = await graph.ainvoke(
        Command(resume={"action": "reject", "feedback": "画面改为暖色"}),
        config=config,
    )

    payload = _interrupt_payload(result)
    assert payload["action"] == "review_plan"
    assert "画面改为暖色" in payload["task_plan"]["tasks"][0]["prompt"]
    assert fake_services.planner.feedback == [None, "画面改为暖色"]
    assert fake_services.planner.plan_calls == 2
    assert fake_services.planner.audit_calls == 2
    _assert_no_paid_side_effects(fake_services)
    assert await fake_services.repository.count_operations() == 0


async def test_cancel_ends_without_generation(fake_services: GraphServices):
    graph = build_graph(fake_services, InMemorySaver())
    config = _config("thread-cancel")
    await graph.ainvoke(_input("run-cancel", "thread-cancel"), config=config)

    result = await graph.ainvoke(
        Command(resume={"action": "cancel"}),
        config=config,
    )

    assert result["status"] == "cancelled"
    assert result["approval_decision"]["action"] == "cancel"
    assert "__interrupt__" not in result
    _assert_no_paid_side_effects(fake_services)
    assert await fake_services.repository.count_operations() == 0


async def test_approve_revalidates_then_executes_generation(
    fake_services: GraphServices,
):
    graph = build_graph(fake_services, InMemorySaver())
    config = _config("thread-approve")
    first = await graph.ainvoke(
        _input("run-approve", "thread-approve"),
        config=config,
    )
    plan = _interrupt_payload(first)["task_plan"]

    result = await graph.ainvoke(
        Command(
            resume={
                "action": "approve",
                "selected_task_ids": ["task-video"],
                "tasks": plan["tasks"],
            }
        ),
        config=config,
    )

    assert result["status"] == "succeeded"
    assert result["approval_decision"]["action"] == "approve"
    assert result["approval_revision"] == 7
    assert [task["task_id"] for task in result["approved_tasks"]] == [
        "task-video"
    ]
    json.dumps(result, ensure_ascii=False)
    assert fake_services.image_generator.submit_calls == 0
    assert fake_services.video_generator.submit_calls == 1
    assert fake_services.video_generator.poll_calls == 0
    assert fake_services.delivery_writer.deliver_calls == 1
    assert result["delivery_record"]["status"] == "succeeded"
    assert await fake_services.repository.count_operations() == 1
    events = await fake_services.repository.list_events("run-approve")
    assert ("human_approval", "started") in [
        (event["node"], event["status"]) for event in events
    ]
    assert ("revalidate_approval", "completed") in [
        (event["node"], event["status"]) for event in events
    ]


async def test_reincluded_excluded_asset_survives_real_graph_execution(
    fake_services: GraphServices,
    tmp_path,
):
    second_path = tmp_path / "data" / "second-reference.png"
    second_path.write_bytes(b"fictional-second-reference")
    second_asset = MediaAsset(
        asset_id="asset-2",
        source_block_id="image-2",
        origin="feishu",
        file_token="fictional-second-token",
        local_path=second_path,
        mime_type="image/png",
        size=second_path.stat().st_size,
        sha256="graph-sha-asset-2",
        width=640,
        height=480,
    )
    document = fake_services.document_source.document
    fake_services.document_source.document = document.model_copy(
        update={
            "blocks": [
                *document.blocks,
                DocumentBlock(
                    block_id="image-2",
                    parent_id="page-1",
                    block_type="image",
                    order=3,
                    path=["page-1", "image-2"],
                    image_asset_id="asset-2",
                ),
            ],
            "media_assets": [*document.media_assets, second_asset],
        }
    )

    class Planner:
        async def plan(
            self,
            document,
            descriptions,
            feedback,
            system_prompt=None,
            exact_system_prompt=None,
        ):
            del (
                document,
                descriptions,
                feedback,
                system_prompt,
                exact_system_prompt,
            )
            return TaskPlan(
                tasks=[fake_services.planner.task],
                document_summary="纸船连续漂流视频",
                excluded_assets=[
                    {
                        "asset_id": "asset-2",
                        "reason": "初次规划只保留主体参考图。",
                    }
                ],
            )

        async def audit(self, document, plan):
            del document, plan
            return AuditReport()

    services = replace(fake_services, planner=Planner())
    graph = build_graph(services, InMemorySaver())
    config = _config("thread-reinclude-excluded")
    first = await graph.ainvoke(
        _input("run-reinclude-excluded", "thread-reinclude-excluded"),
        config=config,
    )
    plan = _interrupt_payload(first)["task_plan"]
    edited = dict(plan["tasks"][0])
    edited["reference_images"] = [
        *edited["reference_images"],
        {"asset_id": "asset-2", "role": "reference_image", "order": 2},
    ]

    result = await graph.ainvoke(
        Command(
            resume={
                "action": "approve",
                "selected_task_ids": ["task-video"],
                "tasks": [edited],
            }
        ),
        config=config,
    )

    assert result["status"] == "succeeded"
    assert services.video_generator.submit_calls == 1
    assert services.delivery_writer.deliver_calls == 1
    assert result["approved_plan"]["excluded_assets"] == []
    assert [
        item["asset_id"]
        for item in result["approved_plan"]["tasks"][0]["reference_images"]
    ] == ["asset-1", "asset-2"]


def test_legacy_approved_checkpoint_reconciles_stale_exclusions():
    draft = TaskPlan.model_validate(
        {
            "tasks": [
                {
                    "task_id": "task-video",
                    "task_type": "image_to_video",
                    "title": "纸船漂流",
                    "source_block_ids": ["paragraph-1"],
                    "user_intent": "生成纸船漂流视频",
                    "prompt": "纸船向前漂流。",
                    "aspect_ratio": "16:9",
                    "resolution": "720p",
                    "duration": 5,
                    "reference_mode": "multi_reference",
                    "reference_images": [
                        {
                            "asset_id": "asset-1",
                            "role": "reference_image",
                            "order": 1,
                        }
                    ],
                }
            ],
            "document_summary": "纸船连续漂流视频",
            "excluded_assets": [
                {
                    "asset_id": "asset-2",
                    "reason": "初次规划只保留主体参考图。",
                }
            ],
        }
    )
    approved_task = draft.tasks[0].model_dump(mode="json")
    approved_task["reference_images"].append(
        {
            "asset_id": "asset-2",
            "role": "reference_image",
            "order": 2,
        }
    )

    restored = approved_plan_from_state(
        {
            "draft_plan": draft.model_dump(mode="json"),
            "approved_tasks": [approved_task],
        },
        max_output_count=3,
    )

    assert restored.excluded_assets == []
    assert [
        item.asset_id for item in restored.tasks[0].reference_images
    ] == ["asset-1", "asset-2"]


async def test_delivery_failure_is_terminal_without_discarding_artifacts(
    fake_services: GraphServices,
):
    delivery = _FailingDeliveryWriter()
    services = replace(fake_services, delivery_writer=delivery)
    graph = build_graph(services, InMemorySaver())
    config = _config("thread-delivery-failure")
    first = await graph.ainvoke(
        _input("run-delivery-failure", "thread-delivery-failure"),
        config=config,
    )
    plan = _interrupt_payload(first)["task_plan"]

    result = await graph.ainvoke(
        Command(
            resume={
                "action": "approve",
                "selected_task_ids": ["task-video"],
                "tasks": plan["tasks"],
            }
        ),
        config=config,
    )

    assert delivery.deliver_calls == 1
    assert result["status"] == "delivery_failed"
    assert len(result["artifacts"]) == 1
    assert result["delivery_record"] is None


async def test_approve_replans_if_source_revision_changed(
    fake_services: GraphServices,
):
    graph = build_graph(fake_services, InMemorySaver())
    config = _config("thread-stale")
    first = await graph.ainvoke(
        _input("run-stale", "thread-stale"),
        config=config,
    )
    plan = _interrupt_payload(first)["task_plan"]
    fake_services.document_source.document = (
        fake_services.document_source.document.model_copy(update={"revision": 8})
    )

    result = await graph.ainvoke(
        Command(
            resume={
                "action": "approve",
                "selected_task_ids": ["task-video"],
                "tasks": plan["tasks"],
            }
        ),
        config=config,
    )

    assert _interrupt_payload(result)["document_revision"] == 8
    assert result["approval_decision"] is None
    assert result["approved_tasks"] == []
    _assert_no_paid_side_effects(fake_services)
    assert await fake_services.repository.count_operations() == 0
    events = await fake_services.repository.list_events("run-stale")
    assert ("check_source_revision", "source_changed") in [
        (event["node"], event["status"]) for event in events
    ]


@pytest.mark.parametrize(
    "resume_payload",
    [
        {"action": "unknown"},
        {"action": "reject", "feedback": ""},
        {"action": "cancel", "selected_task_ids": ["task-video"]},
        {"action": "approve", "selected_task_ids": []},
        {"action": "approve", "selected_task_ids": ["missing"]},
        {
            "action": "approve",
            "selected_task_ids": ["task-video", "task-video"],
        },
        {"action": "cancel", "api_key": "fictional-secret-resume"},
    ],
)
async def test_malformed_resume_payload_is_safely_rejected(
    fake_services: GraphServices,
    resume_payload: dict[str, Any],
):
    graph = build_graph(fake_services, InMemorySaver())
    thread_id = "thread-malformed"
    config = _config(thread_id)
    await graph.ainvoke(_input("run-malformed", thread_id), config=config)

    with pytest.raises(AgentError) as raised:
        await graph.ainvoke(Command(resume=resume_payload), config=config)

    detail_json = json.dumps(raised.value.detail.model_dump(mode="json"))
    assert raised.value.detail.category == ErrorCategory.VALIDATION
    assert raised.value.detail.retryable is False
    assert "fictional-secret-resume" not in detail_json
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    _assert_no_paid_side_effects(fake_services)
    assert await fake_services.repository.count_operations() == 0
    events = await fake_services.repository.list_events("run-malformed")
    assert events[-1]["node"] == "human_approval"
    assert events[-1]["status"] == "failed"
    assert "fictional-secret" not in events[-1]["summary"]


async def test_config_thread_id_must_match_state_thread_id(
    fake_services: GraphServices,
):
    graph = build_graph(fake_services, InMemorySaver())

    with pytest.raises(AgentError) as raised:
        await graph.ainvoke(
            _input("run-mismatch", "thread-state"),
            config=_config("thread-config"),
        )

    assert raised.value.detail.category == ErrorCategory.VALIDATION
    assert fake_services.document_source.ingest_calls == 0
    events = await fake_services.repository.list_events("run-mismatch")
    assert [(event["node"], event["status"]) for event in events] == [
        ("ingest_source", "started"),
        ("ingest_source", "failed"),
    ]
    _assert_no_paid_side_effects(fake_services)


async def test_node_failure_records_only_safe_error_summary(
    fake_services: GraphServices,
    monkeypatch: pytest.MonkeyPatch,
):
    secret = "fictional-secret-from-source"

    async def fail_ingest(request: Any):
        del request
        raise RuntimeError(secret)

    monkeypatch.setattr(fake_services.document_source, "ingest", fail_ingest)
    graph = build_graph(fake_services, InMemorySaver())
    config = _config("thread-failure")

    with pytest.raises(AgentError) as raised:
        await graph.ainvoke(
            _input("run-failure", "thread-failure"),
            config=config,
        )

    serialized = json.dumps(raised.value.detail.model_dump(mode="json"))
    assert secret not in serialized
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    events = await fake_services.repository.list_events("run-failure")
    assert [(event["node"], event["status"]) for event in events] == [
        ("ingest_source", "started"),
        ("ingest_source", "failed"),
    ]
    assert secret not in events[-1]["summary"]


async def test_sqlite_checkpointer_is_strict_and_contains_no_secrets(
    fake_services: GraphServices,
):
    settings = fake_services.settings
    config = _config("thread-sqlite")

    async with open_checkpointer(settings) as checkpointer:
        assert isinstance(checkpointer.serde, JsonPlusSerializer)
        assert checkpointer.serde._allowed_msgpack_modules is None
        encoded = checkpointer.serde.dumps_typed(
            _UnsafeSerdeProbe("must-not-rehydrate")
        )
        restored = checkpointer.serde.loads_typed(encoded)
        assert restored == {"value": "must-not-rehydrate"}
        assert not isinstance(restored, _UnsafeSerdeProbe)
        graph = build_graph(fake_services, checkpointer)
        await graph.ainvoke(
            _input("run-sqlite", "thread-sqlite"),
            config=config,
        )
        snapshot = await graph.aget_state(config)
        json.dumps(snapshot.values, ensure_ascii=False)

    checkpoint_bytes = b"".join(
        path.read_bytes()
        for path in settings.checkpoint_db_path.parent.glob(
            f"{settings.checkpoint_db_path.name}*"
        )
        if path.is_file()
    )
    assert checkpoint_bytes
    for secret in (
        b"fictional-lark-key-must-not-persist",
        b"fictional-deepseek-key-must-not-persist",
        b"fictional-claude-key-must-not-persist",
        b"fictional-chiyun-key-must-not-persist",
        b"fictional-ark-key-must-not-persist",
        b"fictional-graph-image-bytes",
        b"fictional-file-token",
    ):
        assert secret not in checkpoint_bytes


async def test_sqlite_checkpoint_resumes_after_saver_lifecycle(
    fake_services: GraphServices,
):
    settings = fake_services.settings
    thread_id = "thread-durable"
    run_id = "run-durable"
    config = _config(thread_id)

    async with open_checkpointer(settings) as first_checkpointer:
        first_graph = build_graph(fake_services, first_checkpointer)
        first = await first_graph.ainvoke(
            _input(run_id, thread_id),
            config=config,
        )
        plan = _interrupt_payload(first)["draft_plan"]

    assert fake_services.document_source.ingest_calls == 1
    assert fake_services.vision_analyzer.calls == 1
    assert fake_services.planner.plan_calls == 1
    assert fake_services.planner.audit_calls == 1

    resume_source = _ResumeOnlyDocumentSource(revision=7)
    resume_vision = _NeverCalledVisionAnalyzer()
    resume_planner = _NeverCalledPlanner()
    resume_services = replace(
        fake_services,
        document_source=resume_source,
        vision_analyzer=resume_vision,
        planner=resume_planner,
    )
    async with open_checkpointer(settings) as second_checkpointer:
        second_graph = build_graph(resume_services, second_checkpointer)
        result = await second_graph.ainvoke(
            Command(
                resume={
                    "action": "approve",
                    "selected_task_ids": ["task-video"],
                    "tasks": plan["tasks"],
                }
            ),
            config=config,
        )

    assert result["status"] == "succeeded"
    assert result["approval_decision"]["action"] == "approve"
    assert resume_source.ingest_calls == 0
    assert resume_source.revision_calls == 1
    assert resume_vision.calls == 0
    assert resume_planner.plan_calls == 0
    assert resume_planner.audit_calls == 0
    events = await fake_services.repository.list_events(run_id)
    for node in (
        "ingest_source",
        "normalize_document",
        "analyze_images",
        "plan_requirements",
        "audit_plan",
        "validate_plan",
    ):
        assert [event["status"] for event in events if event["node"] == node] == [
            "started",
            "completed",
        ]
    assert resume_services.image_generator.submit_calls == 0
    assert resume_services.video_generator.submit_calls == 1
    assert resume_services.video_generator.poll_calls == 0
    assert resume_services.delivery_writer.deliver_calls == 1
    assert await resume_services.repository.count_operations() == 1


async def test_sqlite_checkpoint_restores_personal_prompt_for_replanning(
    fake_services: GraphServices,
):
    settings = fake_services.settings
    thread_id = "thread-prompt-durable"
    config = _config(thread_id)
    prompt_text = "领取时的个人版本 v2"
    planning_prompt = PlanningPromptSnapshot(
        owner_user_id="user-a",
        source="personal",
        version=2,
        prompt_text=prompt_text,
        prompt_sha256=hashlib.sha256(prompt_text.encode("utf-8")).hexdigest(),
    )

    async with open_checkpointer(settings) as first_checkpointer:
        graph = build_graph(fake_services, first_checkpointer)
        await graph.ainvoke(
            _input("run-prompt-durable", thread_id, planning_prompt),
            config=config,
        )

    async with open_checkpointer(settings) as second_checkpointer:
        restored_graph = build_graph(fake_services, second_checkpointer)
        result = await restored_graph.ainvoke(
            Command(
                resume={"action": "reject", "feedback": "重启后重新规划"}
            ),
            config=config,
        )
        restored = await restored_graph.aget_state(config)

    assert result["planning_prompt"] == planning_prompt.model_dump(mode="json")
    assert restored.values["planning_prompt"] == planning_prompt.model_dump(
        mode="json"
    )
    assert fake_services.planner.system_prompts == [prompt_text, prompt_text]
