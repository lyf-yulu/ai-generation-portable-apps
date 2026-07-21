import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from feishu_generation_agent.bitable.mvp_service import BitableMvpService
from feishu_generation_agent.config import Settings
from feishu_generation_agent.domain import BitableLocation, TableTaskStatus
from feishu_generation_agent.graph.runtime import GraphRuntime
from feishu_generation_agent.integrations.feishu_bitable import BitableSchema
from feishu_generation_agent.storage.bitable_tasks import BitableTaskStore
from feishu_generation_agent.storage.files import FileStore
from feishu_generation_agent.storage.repository import Repository


class _RestartBitable:
    async def resolve_location(self, location):
        return location

    async def ensure_schema(self, location):
        return BitableSchema(
            title_field_id="fld-title",
            source_field_id="fld-source",
            executor_field_id="fld-executor",
            result_field_id="fld-result",
        )

    async def list_tasks(self, location, schema):
        return []


class _RestartGraph:
    def __init__(self) -> None:
        self.states: dict[str, dict] = {}
        self.submit_calls = 0
        self.poll_calls = 0
        self.initial_calls: list[dict] = []

    @staticmethod
    def _thread(config: dict) -> str:
        return config["configurable"]["thread_id"]

    async def aget_state(self, config: dict):
        state = self.states.get(self._thread(config), {})
        return SimpleNamespace(values=state, tasks=(), next=())

    async def ainvoke(self, value, *, config: dict):
        if value is not None:
            self.initial_calls.append(value)
            state = {
                **value,
                "status": "waiting_approval",
                "draft_plan": {
                    "document_summary": "恢复的任务",
                    "tasks": [{"task_id": "task-1"}],
                },
                "draft_revision": 1,
                "__interrupt__": [object()],
            }
            self.states[self._thread(config)] = state
            return state
        self.poll_calls += 1
        state = self.states[self._thread(config)]
        state.update(status="succeeded")
        return state


def _settings(tmp_path: Path) -> Settings:
    settings = Settings(
        data_dir=tmp_path / "data",
        outputs_dir=tmp_path / "outputs",
        business_db_path=tmp_path / "business.sqlite3",
        checkpoint_db_path=tmp_path / "checkpoints.sqlite3",
    )
    settings.ensure_paths()
    return settings


def _location() -> BitableLocation:
    return BitableLocation(
        wiki_token="wikiTABLE",
        app_token="appTABLE",
        table_id="tblTABLE",
        view_id="vewTASKS",
        source_url=(
            "https://tenant.feishu.cn/wiki/wikiTABLE"
            "?table=tblTABLE&view=vewTASKS"
        ),
    )


async def _claim(store: BitableTaskStore, run_id: str, thread_id: str):
    return await store.claim(
        app_token="appTABLE",
        table_id="tblTABLE",
        view_id="vewTASKS",
        record_id=f"rec-{run_id}",
        source_url=f"https://tenant.feishu.cn/docx/doc-{run_id}",
        display_text=f"任务 {run_id}",
        claimant_open_id="local-mvp",
        run_id=run_id,
        thread_id=thread_id,
        reply_context={},
    )


async def _restarted_service(
    settings: Settings, graph: _RestartGraph
) -> tuple[BitableMvpService, GraphRuntime, Repository, BitableTaskStore]:
    repository = await Repository.open(settings.business_db_path)
    store = await BitableTaskStore.open(settings.data_dir / "bitable.sqlite3")
    runtime = GraphRuntime(
        graph=graph,
        repository=repository,
        file_store=FileStore(
            settings.data_dir,
            settings.outputs_dir,
            max_bytes=settings.max_download_bytes,
        ),
        settings=settings,
    )
    service = BitableMvpService(
        bitable=_RestartBitable(),
        store=store,
        runtime=runtime,
        location=_location(),
    )
    return service, runtime, repository, store


@pytest.mark.asyncio
async def test_waiting_approval_restart_preserves_gate_and_fingerprint(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    initial_repository = await Repository.open(settings.business_db_path)
    initial_store = await BitableTaskStore.open(
        settings.data_dir / "bitable.sqlite3"
    )
    await _claim(initial_store, "run-approval", "thread-approval")
    await initial_repository.create_run(
        "run-approval",
        "thread-approval",
        "https://tenant.feishu.cn/docx/doc-run-approval",
        status="waiting_approval",
    )
    await initial_store.close()
    await initial_repository.close()

    graph = _RestartGraph()
    graph.states["thread-approval"] = {
        "run_id": "run-approval",
        "thread_id": "thread-approval",
        "source_url": "https://tenant.feishu.cn/docx/doc-run-approval",
        "status": "waiting_approval",
        "draft_plan": {
            "document_summary": "纸船",
            "tasks": [{"task_id": "task-1", "prompt": "雨中纸船"}],
        },
        "draft_revision": 7,
    }
    service, runtime, repository, store = await _restarted_service(settings, graph)
    try:
        resumed = await service.resume_incomplete()
        binding = await store.get_by_run("run-approval")

        assert resumed == ["run-approval"]
        assert graph.submit_calls == 0
        assert graph.poll_calls == 0
        assert binding is not None
        assert binding.status is TableTaskStatus.WAITING_APPROVAL
        assert binding.approval_version == 1
        assert binding.plan_fingerprint is not None

        fingerprint = binding.plan_fingerprint
        await service.close()
        await runtime.close()
        await repository.close()

        service, runtime, repository, store = await _restarted_service(
            settings, graph
        )
        await service.resume_incomplete()
        replayed = await store.get_by_run("run-approval")
        assert replayed is not None
        assert replayed.approval_version == 1
        assert replayed.plan_fingerprint == fingerprint
        assert graph.submit_calls == 0
        assert graph.poll_calls == 0
    finally:
        await service.close()
        await runtime.close()
        await repository.close()


@pytest.mark.asyncio
async def test_restart_before_first_checkpoint_reuses_bitable_request(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    initial_repository = await Repository.open(settings.business_db_path)
    initial_store = await BitableTaskStore.open(
        settings.data_dir / "bitable.sqlite3"
    )
    await _claim(initial_store, "run-before-checkpoint", "thread-before-checkpoint")
    await initial_repository.create_run(
        "run-before-checkpoint",
        "thread-before-checkpoint",
        "https://tenant.feishu.cn/docx/doc-run-before-checkpoint",
        status="running",
    )
    await initial_store.close()
    await initial_repository.close()

    graph = _RestartGraph()
    service, runtime, repository, store = await _restarted_service(settings, graph)
    try:
        await service.resume_incomplete()
        for _ in range(100):
            run = await repository.get_run("run-before-checkpoint")
            if run is not None and run["status"] == "waiting_approval":
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("run did not recover to approval")

        assert len(graph.initial_calls) == 1
        assert graph.initial_calls[0]["trigger_type"] == "bitable"
        assert graph.initial_calls[0]["reply_context"] == {}
        assert graph.submit_calls == 0
        assert graph.poll_calls == 0
        binding = await service.sync_once("run-before-checkpoint")
        assert binding.status is TableTaskStatus.WAITING_APPROVAL
    finally:
        await service.close()
        await runtime.close()
        await repository.close()


@pytest.mark.asyncio
async def test_provider_submitted_restart_polls_without_second_submit(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    initial_repository = await Repository.open(settings.business_db_path)
    initial_store = await BitableTaskStore.open(
        settings.data_dir / "bitable.sqlite3"
    )
    await _claim(initial_store, "run-provider", "thread-provider")
    await initial_store.set_status(
        "run-provider", TableTaskStatus.GENERATING
    )
    await initial_repository.create_run(
        "run-provider",
        "thread-provider",
        "https://tenant.feishu.cn/docx/doc-run-provider",
        status="waiting_provider",
    )
    await initial_store.close()
    await initial_repository.close()

    graph = _RestartGraph()
    graph.states["thread-provider"] = {
        "run_id": "run-provider",
        "thread_id": "thread-provider",
        "source_url": "https://tenant.feishu.cn/docx/doc-run-provider",
        "status": "waiting_provider",
        "execution_records": [
            {
                "task_id": "task-1",
                "provider": "seedance",
                "provider_task_id": "provider-existing",
                "status": "running",
            }
        ],
    }
    service, runtime, repository, store = await _restarted_service(settings, graph)
    try:
        resumed = await service.resume_incomplete()
        await runtime.wait_for_terminal("run-provider", timeout=1)
        binding = await service.sync_once("run-provider")

        assert resumed == ["run-provider"]
        assert graph.submit_calls == 0
        assert graph.poll_calls == 1
        assert binding.status is TableTaskStatus.COMPLETED
        assert [item.record_id for item in await store.list_active("appTABLE", "tblTABLE")] == []
    finally:
        await service.close()
        await runtime.close()
        await repository.close()
