import asyncio
import base64
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from feishu_generation_agent.domain import (
    AuditReport,
    ProviderResult,
    ProviderSubmission,
    TaskPlan,
)
from feishu_generation_agent.domain.errors import (
    AgentError,
    ErrorCategory,
    ErrorDetail,
)
from feishu_generation_agent.graph.builder import build_graph
from feishu_generation_agent.graph.nodes import (
    GraphServices,
    execute_selected_tasks,
    revalidate_approval,
)
from feishu_generation_agent.storage.checkpoints import open_checkpointer
from feishu_generation_agent.storage.files import FileStore
from feishu_generation_agent.storage.provider_results import ProviderResultStore


MP4_FIXTURE = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2"
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class _ScriptedGenerator:
    def __init__(
        self,
        provider: str,
        *,
        submit_result: ProviderSubmission | None = None,
        submit_error: AgentError | None = None,
        polls: list[ProviderSubmission | AgentError] | None = None,
        call_order: list[str] | None = None,
        block_submit: asyncio.Event | None = None,
    ) -> None:
        self.provider = provider
        self.submit_result = submit_result
        self.submit_error = submit_error
        self.polls = list(polls or [])
        self.call_order = call_order
        self.block_submit = block_submit
        self.submit_calls = 0
        self.poll_calls = 0
        self.submission_ids: list[str | None] = []
        self.first_submit = asyncio.Event()

    async def submit(self, task, assets, *, submission_id=None):
        del assets
        self.submit_calls += 1
        self.submission_ids.append(submission_id)
        self.first_submit.set()
        if self.call_order is not None:
            self.call_order.append(f"submit:{task.task_id}")
        if self.block_submit is not None:
            await self.block_submit.wait()
        if self.submit_error is not None:
            raise self.submit_error
        assert self.submit_result is not None
        if self.submit_result.provider_task_id == "__submission_id__":
            assert submission_id is not None
            return self.submit_result.model_copy(
                update={"provider_task_id": submission_id}
            )
        return self.submit_result

    async def poll(self, submission):
        self.poll_calls += 1
        if self.call_order is not None:
            self.call_order.append(f"poll:{submission.provider_task_id}")
        if not self.polls:
            raise AssertionError("unexpected poll")
        result = self.polls.pop(0)
        if isinstance(result, AgentError):
            raise result
        return result


def _submission(
    provider: str,
    official_id: str,
    status: str,
    *,
    result: ProviderResult | None = None,
) -> ProviderSubmission:
    return ProviderSubmission(
        provider=provider,
        provider_task_id=official_id,
        status=status,
        result_items=[] if result is None else [result],
    )


def _video_result() -> ProviderResult:
    return ProviderResult(
        base64_data=base64.b64encode(MP4_FIXTURE).decode("ascii"),
        mime_type="video/mp4",
    )


def _image_task(video_task: dict[str, Any]) -> dict[str, Any]:
    return {
        **video_task,
        "task_id": "task-image",
        "task_type": "image_to_image",
        "image_size": "1024x1024",
        "duration": None,
        "resolution": None,
        "generate_audio": None,
    }


def _transient_error() -> AgentError:
    return AgentError(
        ErrorDetail(
            category=ErrorCategory.TRANSIENT,
            message="temporary provider failure",
            technical_detail="fictional transient",
            retryable=True,
        )
    )


def _chiyun_staging_poll_error() -> AgentError:
    return AgentError(
        ErrorDetail(
            category=ErrorCategory.PROVIDER_TERMINAL,
            message="staged result invalid",
            technical_detail="operation=poll; cause=staging_invalid",
            retryable=False,
        )
    )


def _input(run_id: str, thread_id: str) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "thread_id": thread_id,
        "source_url": "https://fiction.feishu.cn/docx/doc-execution",
        "status": "created",
    }


def _config(thread_id: str) -> dict[str, dict[str, str]]:
    return {"configurable": {"thread_id": thread_id}}


def _interrupt_payload(result: dict[str, Any]) -> dict[str, Any]:
    return result["__interrupt__"][0].value


def _assert_zero_generation(services: GraphServices) -> None:
    assert services.image_generator.submit_calls == 0
    assert services.image_generator.poll_calls == 0
    assert services.video_generator.submit_calls == 0
    assert services.video_generator.poll_calls == 0


@pytest.mark.asyncio
async def test_source_revision_change_clears_approval_and_interrupts_again(
    fake_services: GraphServices,
) -> None:
    graph = build_graph(fake_services, InMemorySaver())
    thread_id = "thread-source-changed"
    config = _config(thread_id)
    first = await graph.ainvoke(
        _input("run-source-changed", thread_id),
        config=config,
    )
    approved_plan = _interrupt_payload(first)["draft_plan"]
    fake_services.document_source.document = (
        fake_services.document_source.document.model_copy(
            update={"revision": 8}
        )
    )

    result = await graph.ainvoke(
        Command(
            resume={
                "action": "approve",
                "selected_task_ids": ["task-video"],
                "tasks": approved_plan["tasks"],
            }
        ),
        config=config,
    )

    payload = _interrupt_payload(result)
    assert payload["document_revision"] == 8
    assert result["approval_decision"] is None
    assert result["approved_tasks"] == []
    assert fake_services.document_source.ingest_calls == 2
    assert fake_services.document_source.revision_calls == 1
    _assert_zero_generation(fake_services)
    assert await fake_services.repository.count_operations() == 0
    events = await fake_services.repository.list_events("run-source-changed")
    assert ("check_source_revision", "source_changed") in [
        (event["node"], event["status"]) for event in events
    ]


@pytest.mark.asyncio
async def test_revalidate_approval_rejects_task_not_in_formal_draft(
    fake_services: GraphServices,
) -> None:
    graph = build_graph(fake_services, InMemorySaver())
    thread_id = "thread-forged-approval"
    config = _config(thread_id)
    await graph.ainvoke(
        _input("run-forged-approval", thread_id),
        config=config,
    )
    snapshot = await graph.aget_state(config)
    state = dict(snapshot.values)
    forged = dict(state["draft_plan"]["tasks"][0])
    forged["task_id"] = "task-forged"
    state.update(
        {
            "approval_decision": {
                "action": "approve",
                "selected_task_ids": ["task-forged"],
                "tasks": [forged],
            },
            "approval_revision": state["document_revision"],
            "approved_tasks": [forged],
        }
    )

    with pytest.raises(AgentError) as caught:
        await revalidate_approval(state, config, services=fake_services)

    assert caught.value.detail.category == ErrorCategory.VALIDATION
    assert "task-forged" not in json.dumps(
        caught.value.detail.model_dump(mode="json"), ensure_ascii=False
    )
    _assert_zero_generation(fake_services)
    assert await fake_services.repository.count_operations() == 0


@pytest.mark.asyncio
async def test_source_revision_read_error_fails_closed_before_generation(
    fake_services: GraphServices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = build_graph(fake_services, InMemorySaver())
    thread_id = "thread-revision-error"
    config = _config(thread_id)
    first = await graph.ainvoke(
        _input("run-revision-error", thread_id),
        config=config,
    )
    plan = _interrupt_payload(first)["draft_plan"]

    async def fail_revision(source_url: str) -> int:
        del source_url
        raise RuntimeError("raw revision read secret")

    monkeypatch.setattr(
        fake_services.document_source,
        "get_revision",
        fail_revision,
    )

    with pytest.raises(AgentError) as caught:
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

    assert caught.value.detail.category == ErrorCategory.TRANSIENT
    assert "raw revision read secret" not in str(caught.value.detail)
    _assert_zero_generation(fake_services)
    assert await fake_services.repository.count_operations() == 0
    events = await fake_services.repository.list_events("run-revision-error")
    assert events[-1]["node"] == "check_source_revision"
    assert events[-1]["status"] == "failed"


@pytest.mark.asyncio
async def test_approved_graph_executes_and_materializes_before_end(
    fake_services: GraphServices,
) -> None:
    video = _ScriptedGenerator(
        "seedance",
        submit_result=_submission(
            "seedance", "seedance-official-1", "succeeded", result=_video_result()
        ),
    )
    services = replace(fake_services, video_generator=video)
    graph = build_graph(services, InMemorySaver())
    thread_id = "thread-execute-success"
    config = _config(thread_id)
    first = await graph.ainvoke(
        _input("run-execute-success", thread_id), config=config
    )
    plan = _interrupt_payload(first)["draft_plan"]

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
    assert video.submit_calls == 1
    assert video.poll_calls == 0
    assert len(video.submission_ids[0]) == 32
    assert len(result["execution_records"]) == 1
    assert result["execution_records"][0]["status"] == "succeeded"
    assert len(result["artifacts"]) == 1
    operation = await services.repository.get_operation(
        "run-execute-success", "task-video", "submit"
    )
    assert operation is not None
    assert operation["phase"] == "succeeded"
    assert operation["official_id"] == "seedance-official-1"
    artifacts = await services.repository.list_artifacts(
        "run-execute-success", task_id="task-video"
    )
    assert len(artifacts) == 1
    assert services.file_store.verify_artifact(
        "run-execute-success", artifacts[0]
    )
    json.dumps(result, ensure_ascii=False)


class _TwoTaskPlanner:
    def __init__(self, tasks: list[dict[str, Any]]) -> None:
        self.tasks = tasks

    async def plan(self, document, descriptions, feedback):
        del document, descriptions, feedback
        return TaskPlan(tasks=self.tasks, document_summary="two selectable tasks")

    async def audit(self, document, plan):
        del document, plan
        return AuditReport()


class _PerTaskVideoGenerator:
    def __init__(self) -> None:
        self.submit_calls = 0
        self.poll_calls = 0
        self.submitted_task_ids: list[str] = []

    async def submit(self, task, assets, *, submission_id=None):
        del assets
        assert isinstance(submission_id, str)
        self.submit_calls += 1
        self.submitted_task_ids.append(task.task_id)
        return _submission(
            "seedance",
            f"official-{task.task_id}",
            "succeeded",
            result=_video_result(),
        )

    async def poll(self, submission):
        del submission
        self.poll_calls += 1
        raise AssertionError("immediate result must not poll")


@pytest.mark.asyncio
async def test_only_selected_task_runs_after_formal_approval(
    fake_services: GraphServices,
) -> None:
    first_task = fake_services.planner.task.model_dump(mode="json")
    second_task = {**first_task, "task_id": "task-video-second"}
    video = _PerTaskVideoGenerator()
    services = replace(
        fake_services,
        planner=_TwoTaskPlanner([first_task, second_task]),
        video_generator=video,
    )
    graph = build_graph(services, InMemorySaver())
    thread_id = "thread-selected-only"
    config = _config(thread_id)
    first = await graph.ainvoke(
        _input("run-selected-only", thread_id), config=config
    )
    plan = _interrupt_payload(first)["draft_plan"]

    result = await graph.ainvoke(
        Command(
            resume={
                "action": "approve",
                "selected_task_ids": ["task-video-second"],
                "tasks": plan["tasks"],
            }
        ),
        config=config,
    )

    assert result["status"] == "succeeded"
    assert video.submitted_task_ids == ["task-video-second"]
    assert [record["task_id"] for record in result["execution_records"]] == [
        "task-video-second"
    ]
    assert await services.repository.get_operation(
        "run-selected-only", "task-video", "submit"
    ) is None


@pytest.mark.asyncio
async def test_preexisting_intent_is_uncertain_without_provider_calls(
    fake_services: GraphServices,
) -> None:
    run_id = "run-preexisting-intent"
    client_id = "1" * 32
    await fake_services.repository.create_submission_intent_if_absent(
        run_id, "task-video", "seedance", client_id
    )
    graph = build_graph(fake_services, InMemorySaver())
    thread_id = "thread-preexisting-intent"
    config = _config(thread_id)
    first = await graph.ainvoke(_input(run_id, thread_id), config=config)
    plan = _interrupt_payload(first)["draft_plan"]

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

    assert result["status"] == "completed_with_errors"
    assert result["execution_records"][0]["status"] == "submission_uncertain"
    _assert_zero_generation(fake_services)
    operation = await fake_services.repository.get_operation(
        run_id, "task-video", "submit"
    )
    assert operation is not None
    assert operation["phase"] == "submission_uncertain"
    assert operation["official_id"] is None


@pytest.mark.asyncio
async def test_seedance_submit_error_is_uncertain_and_never_retried(
    fake_services: GraphServices,
) -> None:
    video = _ScriptedGenerator(
        "seedance",
        submit_error=_transient_error(),
    )
    services = replace(fake_services, video_generator=video)
    graph = build_graph(services, InMemorySaver())
    thread_id = "thread-submit-error"
    config = _config(thread_id)
    first = await graph.ainvoke(
        _input("run-submit-error", thread_id), config=config
    )
    plan = _interrupt_payload(first)["draft_plan"]

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

    assert result["status"] == "completed_with_errors"
    assert result["execution_records"][0]["status"] == "submission_uncertain"
    assert video.submit_calls == 1
    assert video.poll_calls == 0
    operation = await services.repository.get_operation(
        "run-submit-error", "task-video", "submit"
    )
    assert operation is not None
    assert operation["phase"] == "submission_uncertain"
    serialized = json.dumps(result, ensure_ascii=False)
    assert "fictional transient" not in serialized


@pytest.mark.asyncio
async def test_submitted_recovery_retries_poll_only_and_materializes(
    fake_services: GraphServices,
) -> None:
    run_id = "run-submitted-recovery"
    client_id = "2" * 32
    official_id = "seedance-recovery-official"
    created, _ = await fake_services.repository.create_submission_intent_if_absent(
        run_id, "task-video", "seedance", client_id
    )
    assert created
    assert await fake_services.repository.compare_and_set_operation(
        run_id,
        "task-video",
        "submit",
        expected_phase="intent_created",
        expected_client_submission_id=client_id,
        expected_official_id=None,
        phase="submitted",
        official_id=official_id,
    )
    video = _ScriptedGenerator(
        "seedance",
        polls=[
            _transient_error(),
            _submission("seedance", official_id, "running"),
            _submission(
                "seedance", official_id, "succeeded", result=_video_result()
            ),
        ],
    )
    services = replace(fake_services, video_generator=video)
    graph = build_graph(services, InMemorySaver())
    thread_id = "thread-submitted-recovery"
    config = _config(thread_id)
    first = await graph.ainvoke(_input(run_id, thread_id), config=config)
    plan = _interrupt_payload(first)["draft_plan"]

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
    assert video.submit_calls == 0
    assert video.poll_calls == 3
    operation = await services.repository.get_operation(
        run_id, "task-video", "submit"
    )
    assert operation is not None
    assert operation["phase"] == "succeeded"


@pytest.mark.asyncio
async def test_submitted_poll_rejects_changed_provider_identity(
    fake_services: GraphServices,
) -> None:
    run_id = "run-poll-identity"
    client_id = "6" * 32
    official_id = "seedance-expected-official"
    await fake_services.repository.create_submission_intent_if_absent(
        run_id, "task-video", "seedance", client_id
    )
    assert await fake_services.repository.compare_and_set_operation(
        run_id,
        "task-video",
        "submit",
        expected_phase="intent_created",
        expected_client_submission_id=client_id,
        expected_official_id=None,
        phase="submitted",
        official_id=official_id,
    )
    video = _ScriptedGenerator(
        "seedance",
        polls=[
            _submission(
                "seedance",
                "seedance-different-official",
                "succeeded",
                result=_video_result(),
            )
        ],
    )
    services = replace(fake_services, video_generator=video)
    state, config = await _waiting_state(
        services, run_id, "thread-poll-identity"
    )
    state["approved_tasks"] = state["draft_plan"]["tasks"]

    result = await execute_selected_tasks(state, config, services=services)

    assert video.submit_calls == 0
    assert video.poll_calls == 1
    assert result["execution_records"][0]["status"] == "failed"
    assert result["artifacts"] == []


@pytest.mark.asyncio
async def test_concurrent_execution_has_one_submit_and_intent_loser_is_uncertain(
    fake_services: GraphServices,
) -> None:
    blocker = asyncio.Event()
    video = _ScriptedGenerator(
        "seedance",
        submit_result=_submission(
            "seedance", "seedance-concurrent", "succeeded", result=_video_result()
        ),
        block_submit=blocker,
    )
    services = replace(fake_services, video_generator=video)
    graph = build_graph(services, InMemorySaver())
    thread_id = "thread-concurrent-state"
    config = _config(thread_id)
    await graph.ainvoke(
        _input("run-concurrent", thread_id), config=config
    )
    snapshot = await graph.aget_state(config)
    task = snapshot.values["draft_plan"]["tasks"][0]
    state = dict(snapshot.values)
    state["approved_tasks"] = [task]
    state["status"] = "approved"

    winner = asyncio.create_task(
        execute_selected_tasks(state, config, services=services)
    )
    await video.first_submit.wait()
    loser = await execute_selected_tasks(state, config, services=services)
    blocker.set()
    winner_result = await winner

    assert video.submit_calls == 1
    assert video.poll_calls == 0
    assert loser["execution_records"][0]["status"] == "submission_uncertain"
    assert winner_result["execution_records"][0]["status"] == "succeeded"


async def _waiting_state(
    services: GraphServices,
    run_id: str,
    thread_id: str,
) -> tuple[dict[str, Any], dict[str, dict[str, str]]]:
    graph = build_graph(services, InMemorySaver())
    config = _config(thread_id)
    await graph.ainvoke(_input(run_id, thread_id), config=config)
    snapshot = await graph.aget_state(config)
    return dict(snapshot.values), config


@pytest.mark.asyncio
async def test_tasks_execute_serially_and_later_task_survives_partial_failure(
    fake_services: GraphServices,
) -> None:
    state, config = await _waiting_state(
        fake_services, "run-serial-partial", "thread-serial-partial"
    )
    video_task = state["draft_plan"]["tasks"][0]
    image_task = _image_task(video_task)
    state["approved_tasks"] = [image_task, video_task]
    state["draft_plan"] = {
        "tasks": [image_task, video_task],
        "document_summary": "two serial tasks",
    }
    call_order: list[str] = []
    image = _ScriptedGenerator(
        "chiyun",
        submit_result=_submission(
            "chiyun", "__submission_id__", "failed"
        ),
        call_order=call_order,
    )
    video = _ScriptedGenerator(
        "seedance",
        submit_result=_submission(
            "seedance", "seedance-after-failure", "succeeded", result=_video_result()
        ),
        call_order=call_order,
    )
    services = replace(
        fake_services,
        image_generator=image,
        video_generator=video,
    )

    result = await execute_selected_tasks(state, config, services=services)

    assert call_order == ["submit:task-image", "submit:task-video"]
    assert [record["status"] for record in result["execution_records"]] == [
        "failed",
        "succeeded",
    ]
    assert result["status"] == "completed_with_errors"
    assert [artifact["task_id"] for artifact in result["artifacts"]] == [
        "task-video"
    ]


@pytest.mark.asyncio
async def test_exact_output_count_mismatch_fails_task_after_submission(
    fake_services: GraphServices,
) -> None:
    video = _ScriptedGenerator(
        "seedance",
        submit_result=_submission(
            "seedance", "seedance-count-mismatch", "succeeded"
        ),
    )
    services = replace(fake_services, video_generator=video)
    state, config = await _waiting_state(
        services, "run-count-mismatch", "thread-count-mismatch"
    )
    state["approved_tasks"] = state["draft_plan"]["tasks"]

    result = await execute_selected_tasks(state, config, services=services)

    assert result["execution_records"][0]["status"] == "failed"
    assert result["artifacts"] == []
    operation = await services.repository.get_operation(
        "run-count-mismatch", "task-video", "submit"
    )
    assert operation is not None
    assert operation["phase"] == "failed"


@pytest.mark.asyncio
async def test_submitted_poll_timeout_retains_official_id_and_never_submits(
    fake_services: GraphServices,
) -> None:
    run_id = "run-poll-timeout"
    state, config = await _waiting_state(
        fake_services, run_id, "thread-poll-timeout"
    )
    state["approved_tasks"] = state["draft_plan"]["tasks"]
    client_id = "3" * 32
    official_id = "seedance-timeout-official"
    await fake_services.repository.create_submission_intent_if_absent(
        run_id, "task-video", "seedance", client_id
    )
    assert await fake_services.repository.compare_and_set_operation(
        run_id,
        "task-video",
        "submit",
        expected_phase="intent_created",
        expected_client_submission_id=client_id,
        expected_official_id=None,
        phase="submitted",
        official_id=official_id,
    )
    running = _submission("seedance", official_id, "running")
    video = _ScriptedGenerator("seedance", polls=[running] * 4)
    services = replace(fake_services, video_generator=video)

    result = await execute_selected_tasks(state, config, services=services)

    assert video.submit_calls == 0
    assert video.poll_calls == 4
    assert result["execution_records"][0]["status"] == "timed_out"
    operation = await services.repository.get_operation(
        run_id, "task-video", "submit"
    )
    assert operation is not None
    assert operation["phase"] == "timed_out"
    assert operation["official_id"] == official_id


@pytest.mark.asyncio
async def test_valid_artifact_skips_provider_but_corrupt_artifact_polls_only(
    fake_services: GraphServices,
) -> None:
    run_id = "run-artifact-recovery"
    first_video = _ScriptedGenerator(
        "seedance",
        submit_result=_submission(
            "seedance", "seedance-artifact-recovery", "succeeded", result=_video_result()
        ),
    )
    first_services = replace(fake_services, video_generator=first_video)
    state, config = await _waiting_state(
        first_services, run_id, "thread-artifact-recovery"
    )
    state["approved_tasks"] = state["draft_plan"]["tasks"]
    first = await execute_selected_tasks(state, config, services=first_services)
    assert first["execution_records"][0]["status"] == "succeeded"

    never = _ScriptedGenerator("seedance")
    skipped = await execute_selected_tasks(
        state,
        config,
        services=replace(fake_services, video_generator=never),
    )
    assert skipped["execution_records"][0]["status"] == "succeeded"
    assert never.submit_calls == 0
    assert never.poll_calls == 0

    artifact = (await fake_services.repository.list_artifacts(run_id))[0]
    artifact.local_path.write_bytes(b"corrupt")
    recovery = _ScriptedGenerator(
        "seedance",
        polls=[
            _submission(
                "seedance",
                "seedance-artifact-recovery",
                "succeeded",
                result=_video_result(),
            )
        ],
    )
    recovered = await execute_selected_tasks(
        state,
        config,
        services=replace(fake_services, video_generator=recovery),
    )

    assert recovery.submit_calls == 0
    assert recovery.poll_calls == 1
    assert recovered["execution_records"][0]["status"] == "succeeded"
    saved = (await fake_services.repository.list_artifacts(run_id))[0]
    assert fake_services.file_store.verify_artifact(run_id, saved)


class _RecordingDownloader:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def download(self, url: str, *, expected_mime_type: str) -> bytes:
        self.calls.append((url, expected_mime_type))
        return MP4_FIXTURE


@pytest.mark.asyncio
async def test_signed_result_url_marker_reaches_only_downloader(
    fake_services: GraphServices,
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = "SIGNED_MARKER_MUST_NEVER_PERSIST"
    raw_url = f"https://cdn.fictional.test/result.mp4?token={marker}"
    downloader = _RecordingDownloader()
    file_store = FileStore(
        fake_services.settings.data_dir,
        fake_services.settings.outputs_dir,
        max_bytes=fake_services.settings.max_download_bytes,
        result_downloader=downloader,
    )
    video = _ScriptedGenerator(
        "seedance",
        submit_result=_submission(
            "seedance",
            "seedance-signed-result",
            "succeeded",
            result=ProviderResult(
                url=raw_url,
                url_trust="untrusted",
                mime_type="video/mp4",
            ),
        ),
    )
    services = replace(
        fake_services, video_generator=video, file_store=file_store
    )
    thread_id = "thread-signed-result"
    config = _config(thread_id)
    async with open_checkpointer(services.settings) as checkpointer:
        graph = build_graph(services, checkpointer)
        first = await graph.ainvoke(
            _input("run-signed-result", thread_id), config=config
        )
        plan = _interrupt_payload(first)["draft_plan"]
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

    assert downloader.calls == [(raw_url, "video/mp4")]
    serialized = json.dumps(result, ensure_ascii=False)
    assert marker not in serialized
    assert result["artifacts"][0]["provider_url"] == (
        "https://cdn.fictional.test/result.mp4"
    )
    events = await services.repository.list_events("run-signed-result")
    assert marker not in json.dumps(events, ensure_ascii=False)
    operation = await services.repository.get_operation(
        "run-signed-result", "task-video", "submit"
    )
    assert marker not in json.dumps(operation, ensure_ascii=False)
    assert marker not in caplog.text
    database_bytes = services.settings.business_db_path.read_bytes()
    assert marker.encode() not in database_bytes
    checkpoint_bytes = b"".join(
        path.read_bytes()
        for path in services.settings.checkpoint_db_path.parent.glob(
            f"{services.settings.checkpoint_db_path.name}*"
        )
        if path.is_file()
    )
    assert checkpoint_bytes
    assert marker.encode() not in checkpoint_bytes


@pytest.mark.asyncio
async def test_cold_checkpoint_resume_of_submitted_operation_is_poll_only(
    fake_services: GraphServices,
) -> None:
    run_id = "run-cold-submitted"
    thread_id = "thread-cold-submitted"
    config = _config(thread_id)
    settings = fake_services.settings
    official_id = "seedance-cold-official"
    video = _ScriptedGenerator(
        "seedance",
        polls=[
            _submission(
                "seedance", official_id, "succeeded", result=_video_result()
            )
        ],
    )
    services = replace(fake_services, video_generator=video)
    async with open_checkpointer(settings) as first_checkpointer:
        first_graph = build_graph(services, first_checkpointer)
        first = await first_graph.ainvoke(_input(run_id, thread_id), config=config)
        plan = _interrupt_payload(first)["draft_plan"]

    client_id = "4" * 32
    await services.repository.create_submission_intent_if_absent(
        run_id, "task-video", "seedance", client_id
    )
    assert await services.repository.compare_and_set_operation(
        run_id,
        "task-video",
        "submit",
        expected_phase="intent_created",
        expected_client_submission_id=client_id,
        expected_official_id=None,
        phase="submitted",
        official_id=official_id,
    )
    async with open_checkpointer(settings) as second_checkpointer:
        second_graph = build_graph(services, second_checkpointer)
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
    assert video.submit_calls == 0
    assert video.poll_calls == 1


class _ChiyunStagingGenerator:
    def __init__(self, store: ProviderResultStore) -> None:
        self.store = store
        self.submit_calls = 0
        self.poll_calls = 0
        self.submission_ids: list[str] = []

    async def submit(self, task, assets, *, submission_id=None):
        del task, assets
        self.submit_calls += 1
        assert isinstance(submission_id, str)
        self.submission_ids.append(submission_id)
        official_id, staged = self.store.save(
            [(PNG_1X1, "image/png")], provider_task_id=submission_id
        )
        return ProviderSubmission(
            provider="chiyun",
            provider_task_id=official_id,
            status="succeeded",
            result_items=[
                ProviderResult(
                    local_path=staged[0].local_path,
                    mime_type=staged[0].mime_type,
                    size=staged[0].size,
                    sha256=staged[0].sha256,
                )
            ],
        )

    async def poll(self, submission):
        del submission
        self.poll_calls += 1
        raise AssertionError("immediate Chiyun result must not poll")


@pytest.mark.asyncio
async def test_chiyun_local_result_materializes_with_single_client_id(
    fake_services: GraphServices,
    tmp_path: Path,
) -> None:
    provider_store = ProviderResultStore(
        tmp_path / "chiyun-staging", max_item_bytes=1024
    )
    image = _ChiyunStagingGenerator(provider_store)
    file_store = FileStore(
        fake_services.settings.data_dir,
        fake_services.settings.outputs_dir,
        max_bytes=fake_services.settings.max_download_bytes,
        provider_result_store=provider_store,
    )
    services = replace(
        fake_services, image_generator=image, file_store=file_store
    )
    state, config = await _waiting_state(
        services, "run-chiyun-local", "thread-chiyun-local"
    )
    image_task = _image_task(state["draft_plan"]["tasks"][0])
    state["approved_tasks"] = [image_task]
    state["draft_plan"] = {
        "tasks": [image_task],
        "document_summary": "image task",
    }

    result = await execute_selected_tasks(state, config, services=services)

    assert result["execution_records"][0]["status"] == "succeeded"
    assert image.submit_calls == 1
    assert image.poll_calls == 0
    operation = await services.repository.get_operation(
        "run-chiyun-local", "task-image", "submit"
    )
    assert operation is not None
    assert operation["client_submission_id"] == image.submission_ids[0]
    assert operation["official_id"] == image.submission_ids[0]
    artifact = (await services.repository.list_artifacts("run-chiyun-local"))[0]
    assert file_store.verify_artifact("run-chiyun-local", artifact)


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["missing", "changed"])
async def test_chiyun_submitted_staging_tamper_fails_without_resubmit(
    fake_services: GraphServices,
    tmp_path: Path,
    tamper: str,
) -> None:
    run_id = f"run-chiyun-{tamper}"
    state, config = await _waiting_state(
        fake_services, run_id, f"thread-chiyun-{tamper}"
    )
    image_task = _image_task(state["draft_plan"]["tasks"][0])
    state["approved_tasks"] = [image_task]
    state["draft_plan"] = {
        "tasks": [image_task],
        "document_summary": "image recovery",
    }
    client_id = "5" * 32
    provider_store = ProviderResultStore(
        tmp_path / f"staging-{tamper}", max_item_bytes=1024
    )
    official_id, staged = provider_store.save(
        [(PNG_1X1, "image/png")], provider_task_id=client_id
    )
    result_item = ProviderResult(
        local_path=staged[0].local_path,
        mime_type=staged[0].mime_type,
        size=staged[0].size,
        sha256=staged[0].sha256,
    )
    if tamper == "missing":
        result_item.local_path.unlink()
    else:
        result_item.local_path.write_bytes(b"changed")
    await fake_services.repository.create_submission_intent_if_absent(
        run_id, "task-image", "chiyun", client_id
    )
    assert await fake_services.repository.compare_and_set_operation(
        run_id,
        "task-image",
        "submit",
        expected_phase="intent_created",
        expected_client_submission_id=client_id,
        expected_official_id=None,
        phase="submitted",
        official_id=official_id,
    )
    poll_result = (
        _chiyun_staging_poll_error()
        if tamper == "missing"
        else _submission(
            "chiyun", official_id, "succeeded", result=result_item
        )
    )
    image = _ScriptedGenerator("chiyun", polls=[poll_result])
    services = replace(
        fake_services,
        image_generator=image,
        file_store=FileStore(
            fake_services.settings.data_dir,
            fake_services.settings.outputs_dir,
            max_bytes=fake_services.settings.max_download_bytes,
            provider_result_store=provider_store,
        ),
    )

    result = await execute_selected_tasks(state, config, services=services)

    assert image.submit_calls == 0
    assert image.poll_calls == 1
    assert result["execution_records"][0]["status"] == "submission_uncertain"
    assert result["artifacts"] == []
    operation = await services.repository.get_operation(
        run_id, "task-image", "submit"
    )
    assert operation is not None
    assert operation["phase"] == "submission_uncertain"
    assert operation["official_id"] == official_id


@pytest.mark.asyncio
async def test_local_validation_failure_creates_no_intent(
    fake_services: GraphServices,
) -> None:
    state, config = await _waiting_state(
        fake_services, "run-local-invalid", "thread-local-invalid"
    )
    invalid = dict(state["draft_plan"]["tasks"][0])
    invalid["reference_images"] = [
        {"asset_id": "missing-asset", "role": "reference_image", "order": 1}
    ]
    state["approved_tasks"] = [invalid]

    with pytest.raises(AgentError) as caught:
        await execute_selected_tasks(state, config, services=fake_services)

    assert caught.value.detail.category == ErrorCategory.VALIDATION
    assert await fake_services.repository.count_operations() == 0
    _assert_zero_generation(fake_services)


@pytest.mark.asyncio
async def test_chiyun_mismatched_official_id_becomes_uncertain(
    fake_services: GraphServices,
) -> None:
    image = _ScriptedGenerator(
        "chiyun",
        submit_result=_submission(
            "chiyun", "not-the-client-id", "succeeded"
        ),
    )
    services = replace(fake_services, image_generator=image)
    state, config = await _waiting_state(
        services, "run-chiyun-id-mismatch", "thread-chiyun-id-mismatch"
    )
    image_task = _image_task(state["draft_plan"]["tasks"][0])
    state["approved_tasks"] = [image_task]
    state["draft_plan"] = {
        "tasks": [image_task],
        "document_summary": "identity mismatch",
    }

    result = await execute_selected_tasks(state, config, services=services)

    assert result["execution_records"][0]["status"] == "submission_uncertain"
    assert image.submit_calls == 1
    assert image.poll_calls == 0
    operation = await services.repository.get_operation(
        "run-chiyun-id-mismatch", "task-image", "submit"
    )
    assert operation is not None
    assert operation["phase"] == "submission_uncertain"
    assert operation["official_id"] is None
