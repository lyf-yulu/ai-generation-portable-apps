import json
from dataclasses import dataclass, fields, replace
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.types import Command

from feishu_generation_agent.domain.errors import AgentError, ErrorCategory
from feishu_generation_agent.graph.builder import build_graph
from feishu_generation_agent.graph.nodes import GraphServices
from feishu_generation_agent.graph.state import AgentState
from feishu_generation_agent.storage.checkpoints import open_checkpointer


_FORMAL_STATE_KEYS = {
    "run_id",
    "thread_id",
    "source_url",
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


def _input(run_id: str, thread_id: str) -> AgentState:
    return {
        "run_id": run_id,
        "thread_id": thread_id,
        "source_url": "https://fiction.feishu.cn/docx/doc-graph",
        "status": "created",
    }


def _config(thread_id: str) -> dict[str, dict[str, str]]:
    return {"configurable": {"thread_id": thread_id}}


def _interrupt_payload(result: dict[str, Any]) -> dict[str, Any]:
    return result["__interrupt__"][0].value


def _assert_no_paid_side_effects(services: GraphServices) -> None:
    assert services.image_generator.submit_calls == 0
    assert services.image_generator.poll_calls == 0
    assert services.video_generator.submit_calls == 0
    assert services.video_generator.poll_calls == 0
    assert services.delivery_writer.deliver_calls == 0


def test_agent_state_and_graph_services_contracts_are_stable():
    assert AgentState.__total__ is False
    assert _FORMAL_STATE_KEYS <= set(AgentState.__annotations__)
    assert {
        "run_id",
        "thread_id",
        "source_url",
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
    ]


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
    assert fake_services.delivery_writer.deliver_calls == 0
    assert await fake_services.repository.count_operations() == 1
    events = await fake_services.repository.list_events("run-approve")
    assert ("human_approval", "started") in [
        (event["node"], event["status"]) for event in events
    ]
    assert ("revalidate_approval", "completed") in [
        (event["node"], event["status"]) for event in events
    ]


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
    assert resume_services.delivery_writer.deliver_calls == 0
    assert await resume_services.repository.count_operations() == 1
