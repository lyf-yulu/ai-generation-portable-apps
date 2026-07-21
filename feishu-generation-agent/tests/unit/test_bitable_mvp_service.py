import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from feishu_generation_agent.bitable.mvp_service import BitableMvpService
from feishu_generation_agent.config import Settings
from feishu_generation_agent.domain import (
    BitableLocation,
    BitableTaskSummary,
    RequirementRequest,
    TableTaskStatus,
)
from feishu_generation_agent.graph.runtime import GraphRuntime, RunConflict
from feishu_generation_agent.integrations.feishu_bitable import BitableSchema
from feishu_generation_agent.storage.bitable_tasks import (
    BitableTaskStore,
    TaskAlreadyClaimed,
)
from feishu_generation_agent.storage.files import FileStore
from feishu_generation_agent.storage.repository import Repository


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


def _task(
    record_id: str = "rec-1",
    *,
    executor_open_ids: list[str] | None = None,
    has_result: bool = False,
) -> BitableTaskSummary:
    return BitableTaskSummary(
        record_id=record_id,
        display_text=f"任务 {record_id}",
        source_url=f"https://tenant.feishu.cn/docx/doc{record_id}",
        executor_open_ids=executor_open_ids or [],
        has_result=has_result,
    )


class _FakeBitable:
    def __init__(self, tasks: list[BitableTaskSummary]) -> None:
        self.tasks = tasks
        self.prepare_calls = 0

    async def resolve_location(self, location: BitableLocation) -> BitableLocation:
        self.prepare_calls += 1
        return location.model_copy(update={"app_token": "appTABLE"})

    async def ensure_schema(self, location: BitableLocation) -> BitableSchema:
        assert location.app_token == "appTABLE"
        return BitableSchema(
            title_field_id="fld-title",
            source_field_id="fld-source",
            executor_field_id="fld-executor",
            result_field_id="fld-result",
        )

    async def list_tasks(self, location, schema):
        assert location.app_token == "appTABLE"
        assert schema.result_field_id == "fld-result"
        return list(self.tasks)


class _FakeRuntime:
    def __init__(self, order: list[str] | None = None) -> None:
        self.order = order if order is not None else []
        self.started: list[tuple[RequirementRequest, str, str]] = []
        self.views: dict[str, dict] = {}
        self.retry_calls: list[str] = []
        self.resume_calls = 0

    async def start_run(self, request, *, run_id=None, thread_id=None):
        self.order.append("runtime.start")
        assert run_id is not None
        assert thread_id is not None
        self.started.append((request, run_id, thread_id))
        self.views.setdefault(
            run_id,
            {
                "run_id": run_id,
                "thread_id": thread_id,
                "status": "running",
                "approval": {},
            },
        )
        return run_id

    async def get_run_view(self, run_id: str):
        return self.views[run_id]

    async def retry_delivery(self, run_id: str):
        self.retry_calls.append(run_id)

    async def resume_pending_runs(self):
        self.resume_calls += 1


class _RecordingStore:
    def __init__(self, store: BitableTaskStore, order: list[str]) -> None:
        self._store = store
        self._order = order

    def __getattr__(self, name):
        return getattr(self._store, name)

    async def claim(self, **kwargs):
        self._order.append("store.claim")
        return await self._store.claim(**kwargs)


async def _service(tmp_path: Path, *, tasks=None, runtime=None):
    store = await BitableTaskStore.open(tmp_path / "bitable.sqlite3")
    service = BitableMvpService(
        bitable=_FakeBitable(tasks or [_task()]),
        store=store,
        runtime=runtime or _FakeRuntime(),
        location=_location(),
    )
    return service, store


@pytest.mark.asyncio
async def test_scan_excludes_results_and_active_claims_but_ignores_executor(
    tmp_path: Path,
) -> None:
    tasks = [
        _task("rec-open", executor_open_ids=["ou_someone_else"]),
        _task("rec-result", has_result=True),
        _task("rec-active"),
    ]
    service, store = await _service(tmp_path, tasks=tasks)
    await store.claim(
        app_token="appTABLE",
        table_id="tblTABLE",
        view_id="vewTASKS",
        record_id="rec-active",
        source_url="https://tenant.feishu.cn/docx/docrec-active",
        display_text="任务 rec-active",
        claimant_open_id="local-mvp",
        run_id="run-active",
        thread_id="thread-active",
        reply_context={},
    )
    try:
        scanned = await service.scan()

        assert [task.record_id for task in scanned] == ["rec-open"]
        assert scanned[0].executor_open_ids == ["ou_someone_else"]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_claim_reserves_before_runtime_start_with_local_identity(
    tmp_path: Path,
) -> None:
    order: list[str] = []
    inner = await BitableTaskStore.open(tmp_path / "bitable.sqlite3")
    runtime = _FakeRuntime(order)
    service = BitableMvpService(
        bitable=_FakeBitable([_task()]),
        store=_RecordingStore(inner, order),
        runtime=runtime,
        location=_location(),
    )
    try:
        run_id = await service.claim("rec-1")
        binding = await inner.get_by_run(run_id)

        assert order == ["store.claim", "runtime.start"]
        assert binding is not None
        assert binding.claimant_open_id == "local-mvp"
        assert binding.reply_context == {}
        request, started_run_id, started_thread_id = runtime.started[0]
        assert started_run_id == binding.run_id
        assert started_thread_id == binding.thread_id
        assert request == RequirementRequest(
            source_url="https://tenant.feishu.cn/docx/docrec-1",
            trigger_type="bitable",
            reply_context={},
        )
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_two_concurrent_services_claim_exactly_once(tmp_path: Path) -> None:
    path = tmp_path / "bitable.sqlite3"
    stores = [await BitableTaskStore.open(path), await BitableTaskStore.open(path)]
    runtimes = [_FakeRuntime(), _FakeRuntime()]
    services = [
        BitableMvpService(
            bitable=_FakeBitable([_task()]),
            store=stores[index],
            runtime=runtimes[index],
            location=_location(),
        )
        for index in range(2)
    ]
    try:
        results = await asyncio.gather(
            *(service.claim("rec-1") for service in services),
            return_exceptions=True,
        )

        assert sum(isinstance(item, str) for item in results) == 1
        assert sum(isinstance(item, TaskAlreadyClaimed) for item in results) == 1
        assert sum(len(runtime.started) for runtime in runtimes) == 1
    finally:
        await asyncio.gather(*(service.close() for service in services))


@pytest.mark.asyncio
async def test_sync_persists_approval_version_and_release_semantics(
    tmp_path: Path,
) -> None:
    runtime = _FakeRuntime()
    service, store = await _service(tmp_path, runtime=runtime)
    try:
        run_id = await service.claim("rec-1")
        runtime.views[run_id] = {
            "run_id": run_id,
            "status": "waiting_approval",
            "approval": {
                "revision": 3,
                "tasks": [{"task_id": "task-1", "prompt": "纸船"}],
            },
        }

        waiting = await service.sync_once(run_id)
        replayed = await service.sync_once(run_id)

        assert waiting.status is TableTaskStatus.WAITING_APPROVAL
        assert waiting.approval_version == 1
        assert waiting.plan_fingerprint is not None
        assert replayed.approval_version == 1

        runtime.views[run_id]["status"] = "delivery_failed"
        delivery_failed = await service.sync_once(run_id)
        assert delivery_failed.status is TableTaskStatus.WRITEBACK_FAILED
        with pytest.raises(TaskAlreadyClaimed):
            await store.claim(
                app_token="appTABLE",
                table_id="tblTABLE",
                view_id="vewTASKS",
                record_id="rec-1",
                source_url="https://tenant.feishu.cn/docx/docrec-1",
                display_text="任务 rec-1",
                claimant_open_id="local-mvp",
                run_id="run-other",
                thread_id="thread-other",
                reply_context={},
            )

        await service.retry_delivery(run_id)
        assert runtime.retry_calls == [run_id]
        assert (await store.get_by_run(run_id)).status is TableTaskStatus.WRITING_BACK

        runtime.views[run_id]["status"] = "succeeded"
        completed = await service.sync_once(run_id)
        assert completed.status is TableTaskStatus.COMPLETED
        assert (await service.scan())[0].record_id == "rec-1"
    finally:
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime_status", ["failed", "cancelled", "completed_with_errors"])
async def test_terminal_status_releases_claim(
    tmp_path: Path, runtime_status: str
) -> None:
    runtime = _FakeRuntime()
    service, store = await _service(tmp_path, runtime=runtime)
    try:
        run_id = await service.claim("rec-1")
        runtime.views[run_id]["status"] = runtime_status

        binding = await service.sync_once(run_id)

        expected = (
            TableTaskStatus.COMPLETED
            if runtime_status == "completed_with_errors"
            else TableTaskStatus.FAILED
        )
        assert binding.status is expected
        assert (await service.scan())[0].record_id == "rec-1"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_retry_delivery_rejects_non_delivery_failure(tmp_path: Path) -> None:
    service, _ = await _service(tmp_path)
    try:
        run_id = await service.claim("rec-1")
        with pytest.raises(RunConflict, match="交付失败"):
            await service.retry_delivery(run_id)
    finally:
        await service.close()


class _StartGraph:
    def __init__(self) -> None:
        self.calls: list[tuple[dict, dict]] = []

    async def ainvoke(self, value, *, config):
        self.calls.append((value, config))
        return {**value, "status": "waiting_approval", "__interrupt__": [object()]}


@pytest.mark.asyncio
async def test_runtime_accepts_reserved_ids_and_repeat_start_is_idempotent(
    tmp_path: Path,
) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        outputs_dir=tmp_path / "outputs",
        business_db_path=tmp_path / "business.sqlite3",
        checkpoint_db_path=tmp_path / "checkpoint.sqlite3",
    )
    settings.ensure_paths()
    repository = await Repository.open(settings.business_db_path)
    graph = _StartGraph()
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
    request = RequirementRequest(
        source_url="https://tenant.feishu.cn/docx/doc-reserved",
        trigger_type="bitable",
    )
    try:
        first = await runtime.start_run(
            request,
            run_id="run-reserved",
            thread_id="thread-reserved",
        )
        second = await runtime.start_run(
            request,
            run_id="run-reserved",
            thread_id="thread-reserved",
        )
        for _ in range(100):
            if (await repository.get_run("run-reserved"))["status"] == "waiting_approval":
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("reserved run did not reach approval")

        assert first == second == "run-reserved"
        assert len(graph.calls) == 1
        value, config = graph.calls[0]
        assert value["trigger_type"] == "bitable"
        assert config == {"configurable": {"thread_id": "thread-reserved"}}

        with pytest.raises(RunConflict, match="预留"):
            await runtime.start_run(
                request,
                run_id="run-reserved",
                thread_id="different-thread",
            )
    finally:
        await runtime.close()
        await repository.close()


@pytest.mark.asyncio
async def test_runtime_without_reserved_ids_keeps_existing_behavior(
    tmp_path: Path,
) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        outputs_dir=tmp_path / "outputs",
        business_db_path=tmp_path / "business.sqlite3",
        checkpoint_db_path=tmp_path / "checkpoint.sqlite3",
    )
    settings.ensure_paths()
    repository = await Repository.open(settings.business_db_path)
    runtime = GraphRuntime(
        graph=_StartGraph(),
        repository=repository,
        file_store=FileStore(
            settings.data_dir,
            settings.outputs_dir,
            max_bytes=settings.max_download_bytes,
        ),
        settings=settings,
    )
    try:
        run_id = await runtime.start_run(
            RequirementRequest(
                source_url="https://tenant.feishu.cn/docx/doc-local"
            )
        )
        run = await repository.get_run(run_id)
        assert run is not None
        assert run["thread_id"] != run_id
    finally:
        await runtime.close()
        await repository.close()
