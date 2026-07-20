from pathlib import Path
from types import SimpleNamespace

import pytest

from feishu_generation_agent.config import Settings
from feishu_generation_agent.domain.artifact import DeliveryRecord
from feishu_generation_agent.graph.runtime import GraphRuntime, RunNotFound
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
async def test_retry_delivery_does_not_resume_generation(tmp_path: Path) -> None:
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

    await runtime.retry_delivery("run-delivery")
    final = await runtime.wait_for_terminal("run-delivery", timeout=1)

    assert final["status"] == "completed_with_errors"
    assert writer.retry_calls == 1
    assert graph.resume_calls == 0
    assert graph.submit_calls == 0
    assert graph.poll_calls == 0
    await runtime.close()
    await repository.close()


@pytest.mark.asyncio
async def test_restart_during_delivery_retry_continues_delivery_only(
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
        status="delivering",
    )

    await runtime.resume_pending_runs()
    final = await runtime.wait_for_terminal("run-delivery", timeout=1)

    assert final["status"] == "completed_with_errors"
    assert writer.retry_calls == 1
    assert graph.resume_calls == 0
    assert graph.submit_calls == 0
    assert graph.poll_calls == 0
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
