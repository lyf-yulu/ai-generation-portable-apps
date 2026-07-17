import json
import os
from dataclasses import fields
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from feishu_generation_agent.domain.errors import AgentError, ErrorCategory
from feishu_generation_agent.graph.builder import build_graph
from feishu_generation_agent.graph.nodes import GraphServices
from feishu_generation_agent.graph.state import AgentState
from feishu_generation_agent.storage.checkpoints import open_checkpointer


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
    assert set(AgentState.__annotations__) == {
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
    }
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

    json.dumps(snapshot.values, ensure_ascii=False)
    assert snapshot.values["status"] == "waiting_approval"
    assert snapshot.values["source_document"]["media_assets"][0][
        "file_token"
    ] is None
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


async def test_approve_revalidates_but_does_not_generate(
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

    assert result["status"] == "approved"
    assert result["approval_decision"]["action"] == "approve"
    assert result["approval_revision"] == 7
    assert [task["task_id"] for task in result["approved_tasks"]] == [
        "task-video"
    ]
    json.dumps(result, ensure_ascii=False)
    _assert_no_paid_side_effects(fake_services)
    assert await fake_services.repository.count_operations() == 0
    events = await fake_services.repository.list_events("run-approve")
    assert ("human_approval", "started") in [
        (event["node"], event["status"]) for event in events
    ]
    assert ("revalidate_approval", "completed") in [
        (event["node"], event["status"]) for event in events
    ]


async def test_approve_fails_safely_if_source_revision_changed(
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

    with pytest.raises(AgentError) as raised:
        await graph.ainvoke(
            Command(
                resume={
                    "action": "approve",
                    "selected_task_ids": ["task-video"],
                    "tasks": plan["tasks"],
                }
            ),
            config=config,
        )

    assert raised.value.detail.category == ErrorCategory.VALIDATION
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    _assert_no_paid_side_effects(fake_services)
    assert await fake_services.repository.count_operations() == 0
    events = await fake_services.repository.list_events("run-stale")
    assert (events[-1]["node"], events[-1]["status"]) == (
        "revalidate_approval",
        "failed",
    )


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
    assert os.environ["LANGGRAPH_STRICT_MSGPACK"] == "true"
    settings = fake_services.settings
    config = _config("thread-sqlite")

    async with open_checkpointer(settings) as checkpointer:
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
