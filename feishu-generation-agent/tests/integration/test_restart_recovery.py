import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from feishu_generation_agent.config import Settings
from feishu_generation_agent.domain.artifact import DeliveryRecord
from feishu_generation_agent.domain.document import build_planning_prompt_snapshot
from feishu_generation_agent.graph.runtime import (
    GraphRuntime,
    RunConflict,
    RunNotFound,
    RunValidationError,
)
from feishu_generation_agent.integrations.planner import planner_system_prompt
from feishu_generation_agent.storage.files import FileStore
from feishu_generation_agent.storage.repository import Repository


class _RecoveryGraph:
    def __init__(self) -> None:
        self.states: dict[str, dict] = {}
        self.submit_calls = 0
        self.poll_calls = 0
        self.resume_calls = 0
        self.deleted_threads: list[str] = []
        self.checkpointer = self

    @staticmethod
    def _thread(config: dict) -> str:
        return config["configurable"]["thread_id"]

    async def aget_state(self, config: dict):
        state = self.states.get(self._thread(config), {})
        return SimpleNamespace(values=state, tasks=(), next=("execute_selected_tasks",))

    async def ainvoke(self, value, *, config: dict):
        assert value is None
        self.resume_calls += 1
        self.poll_calls += 1
        thread_id = self._thread(config)
        state = self.states[thread_id]
        state.update(
            status="succeeded",
            delivery_record={
                "document_id": "delivery-doc",
                "document_url": "https://fiction.feishu.cn/docx/delivery-doc",
                "status": "succeeded",
                "uploaded_artifact_ids": [],
            },
        )
        return state

    async def aupdate_state(self, config: dict, values: dict, **kwargs):
        del kwargs
        self.states[self._thread(config)].update(values)

    async def adelete_thread(self, thread_id: str) -> None:
        self.deleted_threads.append(thread_id)
        self.states.pop(thread_id, None)


class _RetryDeliveryWriter:
    def __init__(self) -> None:
        self.retry_calls = 0

    async def retry_delivery(self, run_id: str) -> DeliveryRecord:
        assert run_id == "run-delivery"
        self.retry_calls += 1
        return DeliveryRecord(
            document_id="delivery-doc",
            document_url="https://fiction.feishu.cn/docx/delivery-doc",
            status="succeeded",
        )


class _BlockingPromptCheckGraph(_RecoveryGraph):
    def __init__(self) -> None:
        super().__init__()
        self.first_prompt_check_started = asyncio.Event()
        self.release_first_prompt_check = asyncio.Event()
        self._blocked_once = False

    async def aget_state(self, config: dict):
        if not self._blocked_once:
            self._blocked_once = True
            self.first_prompt_check_started.set()
            await self.release_first_prompt_check.wait()
        return await super().aget_state(config)


class _FailingWorkerPromptCheckGraph(_RecoveryGraph):
    def __init__(self) -> None:
        super().__init__()
        self.prompt_checks = 0

    async def aget_state(self, config: dict):
        self.prompt_checks += 1
        if self.prompt_checks == 2:
            raise RuntimeError("fictional checkpoint read failure")
        return await super().aget_state(config)


def _prime_prompt() -> dict:
    return build_planning_prompt_snapshot(
        owner_user_id="prime-local",
        source="prime",
        version=0,
        prompt_text=planner_system_prompt(),
    ).model_dump(mode="json")


async def _runtime(tmp_path: Path, graph: _RecoveryGraph, delivery_writer=None):
    settings = Settings(
        data_dir=tmp_path / "data",
        outputs_dir=tmp_path / "outputs",
        business_db_path=tmp_path / "business.sqlite3",
        checkpoint_db_path=tmp_path / "checkpoints.sqlite3",
    )
    settings.ensure_paths()
    repository = await Repository.open(settings.business_db_path)
    runtime = GraphRuntime(
        graph=graph,
        repository=repository,
        file_store=FileStore(
            settings.data_dir,
            settings.outputs_dir,
            max_bytes=settings.max_download_bytes,
        ),
        settings=settings,
        delivery_writer=delivery_writer,
    )
    return runtime, repository, settings


@pytest.mark.asyncio
async def test_restart_reuses_checkpoint_and_polls_without_resubmit(
    tmp_path: Path,
) -> None:
    graph = _RecoveryGraph()
    graph.states["thread-recovery"] = {
        "run_id": "run-recovery",
        "thread_id": "thread-recovery",
        "status": "waiting_provider",
        "planning_prompt": _prime_prompt(),
    }
    runtime, repository, _ = await _runtime(tmp_path, graph)
    await repository.create_run(
        "run-recovery",
        "thread-recovery",
        "https://acme.feishu.cn/docx/doccn123",
        status="waiting_provider",
    )

    await runtime.resume_pending_runs()
    final = await runtime.wait_for_terminal("run-recovery", timeout=1)

    assert final["status"] == "succeeded"
    assert graph.resume_calls == 1
    assert graph.submit_calls == 0
    assert graph.poll_calls == 1
    await runtime.close()
    await repository.close()


@pytest.mark.asyncio
async def test_restart_fails_closed_when_checkpoint_has_no_prompt_snapshot(
    tmp_path: Path,
) -> None:
    graph = _RecoveryGraph()
    graph.states["thread-legacy"] = {
        "run_id": "run-legacy",
        "thread_id": "thread-legacy",
        "status": "waiting_provider",
    }
    runtime, repository, _ = await _runtime(tmp_path, graph)
    await repository.create_run(
        "run-legacy",
        "thread-legacy",
        "https://acme.feishu.cn/docx/doccn123",
        status="waiting_provider",
    )

    await runtime.resume_pending_runs()
    final = await runtime.wait_for_terminal("run-legacy", timeout=1)

    assert final["status"] == "failed"
    assert graph.resume_calls == 0
    events = await repository.list_events("run-legacy")
    assert events[-1]["node"] == "planning_prompt"
    assert events[-1]["status"] == "failed"
    await runtime.close()
    await repository.close()


@pytest.mark.asyncio
async def test_retry_delivery_does_not_resume_generation(tmp_path: Path) -> None:
    graph = _RecoveryGraph()
    graph.states["thread-delivery"] = {
        "run_id": "run-delivery",
        "thread_id": "thread-delivery",
        "status": "delivery_failed",
        "planning_prompt": _prime_prompt(),
    }
    writer = _RetryDeliveryWriter()
    runtime, repository, _ = await _runtime(tmp_path, graph, writer)
    await repository.create_run(
        "run-delivery",
        "thread-delivery",
        "https://acme.feishu.cn/docx/doccn123",
        status="delivery_failed",
    )

    await runtime.retry_delivery("run-delivery")
    final = await runtime.wait_for_terminal("run-delivery", timeout=1)

    assert final["status"] == "failed"
    assert writer.retry_calls == 1
    assert graph.resume_calls == 0
    assert graph.submit_calls == 0
    assert graph.poll_calls == 0
    await runtime.close()
    await repository.close()


@pytest.mark.asyncio
async def test_concurrent_delivery_retries_reserve_one_external_write(
    tmp_path: Path,
) -> None:
    graph = _BlockingPromptCheckGraph()
    graph.states["thread-delivery"] = {
        "run_id": "run-delivery",
        "thread_id": "thread-delivery",
        "status": "delivery_failed",
        "planning_prompt": _prime_prompt(),
    }
    writer = _RetryDeliveryWriter()
    runtime, repository, _ = await _runtime(tmp_path, graph, writer)
    await repository.create_run(
        "run-delivery",
        "thread-delivery",
        "https://acme.feishu.cn/docx/doccn123",
        status="delivery_failed",
    )

    first = asyncio.create_task(runtime.retry_delivery("run-delivery"))
    await graph.first_prompt_check_started.wait()
    second = asyncio.create_task(runtime.retry_delivery("run-delivery"))
    await asyncio.sleep(0)
    graph.release_first_prompt_check.set()
    outcomes = await asyncio.gather(first, second, return_exceptions=True)
    for _ in range(100):
        if not runtime._background_tasks:
            break
        await asyncio.sleep(0.01)

    assert sum(outcome is None for outcome in outcomes) == 1
    assert sum(isinstance(outcome, RunConflict) for outcome in outcomes) == 1
    assert writer.retry_calls == 1
    await runtime.close()
    await repository.close()


@pytest.mark.asyncio
async def test_delivery_worker_checkpoint_error_reaches_safe_terminal_state(
    tmp_path: Path,
) -> None:
    graph = _FailingWorkerPromptCheckGraph()
    graph.states["thread-delivery"] = {
        "run_id": "run-delivery",
        "thread_id": "thread-delivery",
        "status": "delivery_failed",
        "planning_prompt": _prime_prompt(),
    }
    writer = _RetryDeliveryWriter()
    runtime, repository, _ = await _runtime(tmp_path, graph, writer)
    await repository.create_run(
        "run-delivery",
        "thread-delivery",
        "https://acme.feishu.cn/docx/doccn123",
        status="delivery_failed",
    )

    try:
        await runtime.retry_delivery("run-delivery")
        for _ in range(100):
            if not runtime._background_tasks:
                break
            await asyncio.sleep(0.01)
        run = await repository.get_run("run-delivery")
        events = await repository.list_events("run-delivery")
    finally:
        await runtime.close()
        await repository.close()

    assert run is not None and run["status"] == "delivery_failed"
    assert writer.retry_calls == 0
    assert events[-1]["node"] == "deliver_to_feishu"
    assert events[-1]["status"] == "failed"


@pytest.mark.asyncio
async def test_restart_during_delivery_retry_continues_delivery_only(
    tmp_path: Path,
) -> None:
    graph = _RecoveryGraph()
    graph.states["thread-delivery"] = {
        "run_id": "run-delivery",
        "thread_id": "thread-delivery",
        "status": "delivery_failed",
        "planning_prompt": _prime_prompt(),
    }
    writer = _RetryDeliveryWriter()
    runtime, repository, _ = await _runtime(tmp_path, graph, writer)
    await repository.create_run(
        "run-delivery",
        "thread-delivery",
        "https://acme.feishu.cn/docx/doccn123",
        status="delivering",
    )

    await runtime.resume_pending_runs()
    final = await runtime.wait_for_terminal("run-delivery", timeout=1)

    assert final["status"] == "failed"
    assert writer.retry_calls == 1
    assert graph.resume_calls == 0
    assert graph.submit_calls == 0
    assert graph.poll_calls == 0
    await runtime.close()
    await repository.close()


@pytest.mark.asyncio
async def test_delivery_recovery_without_prompt_fails_before_external_write(
    tmp_path: Path,
) -> None:
    graph = _RecoveryGraph()
    graph.states["thread-delivery"] = {
        "run_id": "run-delivery",
        "thread_id": "thread-delivery",
        "status": "delivering",
    }
    writer = _RetryDeliveryWriter()
    runtime, repository, _ = await _runtime(tmp_path, graph, writer)
    await repository.create_run(
        "run-delivery",
        "thread-delivery",
        "https://acme.feishu.cn/docx/doccn123",
        status="delivering",
    )

    await runtime.resume_pending_runs()
    final = await runtime.wait_for_terminal("run-delivery", timeout=1)

    assert final["status"] == "failed"
    assert writer.retry_calls == 0
    events = await repository.list_events("run-delivery")
    assert events[-1]["node"] == "planning_prompt"
    await runtime.close()
    await repository.close()


@pytest.mark.asyncio
async def test_manual_delivery_retry_without_prompt_fails_before_external_write(
    tmp_path: Path,
) -> None:
    graph = _RecoveryGraph()
    graph.states["thread-delivery"] = {
        "run_id": "run-delivery",
        "thread_id": "thread-delivery",
        "status": "delivery_failed",
    }
    writer = _RetryDeliveryWriter()
    runtime, repository, _ = await _runtime(tmp_path, graph, writer)
    await repository.create_run(
        "run-delivery",
        "thread-delivery",
        "https://acme.feishu.cn/docx/doccn123",
        status="delivery_failed",
    )

    with pytest.raises(RunValidationError, match="提示词快照"):
        await runtime.retry_delivery("run-delivery")

    run = await repository.get_run("run-delivery")
    assert run is not None and run["status"] == "failed"
    assert writer.retry_calls == 0
    await runtime.close()
    await repository.close()


@pytest.mark.asyncio
async def test_delete_waiting_run_removes_rows_files_and_checkpoint(
    tmp_path: Path,
) -> None:
    graph = _RecoveryGraph()
    graph.states["thread-delete"] = {
        "run_id": "run-delete",
        "thread_id": "thread-delete",
        "status": "waiting_approval",
    }
    runtime, repository, settings = await _runtime(tmp_path, graph)
    await repository.create_run(
        "run-delete",
        "thread-delete",
        "https://acme.feishu.cn/docx/doccn123",
        status="waiting_approval",
    )
    data_run = settings.data_dir / "runs" / "run-delete"
    output_run = settings.outputs_dir / "runs" / "run-delete"
    data_run.mkdir(parents=True)
    output_run.mkdir(parents=True)
    (data_run / "source.bin").write_bytes(b"source")
    (output_run / "artifact.bin").write_bytes(b"artifact")

    await runtime.delete_run("run-delete")

    assert await repository.get_run("run-delete") is None
    assert not data_run.exists()
    assert not output_run.exists()
    assert graph.deleted_threads == ["thread-delete"]
    with pytest.raises(RunNotFound):
        await runtime.get_run_view("run-delete")
    await runtime.close()
    await repository.close()
