import pytest

from feishu_generation_agent.bitable.production_service import (
    ProductionBitableService,
    ProductionTaskSource,
)
from feishu_generation_agent.domain.bitable import BitableLocation
from feishu_generation_agent.domain.document import RequirementRequest
from feishu_generation_agent.domain.production_bitable import (
    ProductionSourceSnapshot,
    ProductionTaskSummary,
)
from feishu_generation_agent.graph.runtime import RunConflict, RunValidationError
from feishu_generation_agent.storage.production_tasks import ProductionTaskStore


def _location() -> BitableLocation:
    return BitableLocation(
        wiki_token="wikiProd", app_token="appProd", table_id="tblProd",
        view_id="vewProd", source_url="https://tenant.feishu.cn/wiki/wikiProd?table=tblProd&view=vewProd",
    )


def _task() -> ProductionTaskSummary:
    return ProductionTaskSummary(
        record_id="rec-no-maker", display_text="需求 A",
        source_url="https://tenant.feishu.cn/docx/docA", progress="未开始", task_type="动画类",
        snapshot=ProductionSourceSnapshot(
            requirement_name="需求 A", task_type="动画类", requirement_attachment="https://tenant.feishu.cn/docx/docA"
        ),
    )


def _portrait_task() -> ProductionTaskSummary:
    return ProductionTaskSummary(
        record_id="rec-portrait",
        display_text="真人需求",
        source_url="https://tenant.feishu.cn/docx/docPortrait",
        progress="未开始",
        task_type="真人类",
        snapshot=ProductionSourceSnapshot(
            requirement_name="真人需求",
            task_type="真人类",
            requirement_attachment="https://tenant.feishu.cn/docx/docPortrait",
        ),
    )


def _category_sources() -> dict[str, ProductionTaskSource]:
    return {
        "animation": ProductionTaskSource(
            _location().model_copy(update={"view_id": "vewAnimation"}),
            "动画类",
        ),
        "portrait": ProductionTaskSource(
            _location().model_copy(update={"view_id": "vewPortrait"}),
            "真人类",
        ),
    }


class _MixedCategoryBitable:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def ensure_schema(self, location):
        return object()

    async def list_tasks(self, location, schema, *, include_completed):
        self.calls.append(location.view_id)
        return [_task(), _portrait_task()]


class _Runtime:
    async def start_run(self, request, *, run_id=None, thread_id=None):
        return run_id


async def _production_service(
    tmp_path,
    *,
    bitable,
    sources,
    enabled_task_types=frozenset({"动画类", "真人类"}),
):
    store = await ProductionTaskStore.open(tmp_path / "production.sqlite3")
    service = ProductionBitableService(
        bitable=bitable,
        store=store,
        runtime=_Runtime(),
        sources=sources,
        include_completed_for_test=False,
        enabled_task_types=enabled_task_types,
    )
    return service, store


async def test_service_scans_only_the_requested_category_and_exact_type(
    tmp_path,
) -> None:
    bitable = _MixedCategoryBitable()
    service, store = await _production_service(
        tmp_path,
        bitable=bitable,
        sources=_category_sources(),
    )
    try:
        animation_tasks = await service.scan("animation")
        portrait_tasks = await service.scan("portrait")
    finally:
        await store.close()

    assert [task.task_type for task in animation_tasks] == ["动画类"]
    assert [task.task_type for task in portrait_tasks] == ["真人类"]
    assert bitable.calls == ["vewAnimation", "vewPortrait"]


async def test_portrait_claim_uses_the_portrait_source_location(tmp_path) -> None:
    service, store = await _production_service(
        tmp_path,
        bitable=_MixedCategoryBitable(),
        sources=_category_sources(),
        enabled_task_types=frozenset({"动画类", "真人类"}),
    )
    try:
        run_id = await service.claim("rec-portrait", "portrait")
        binding = await store.get_by_run(run_id)
    finally:
        await store.close()

    assert binding is not None
    assert binding.source_location.view_id == "vewPortrait"
    assert binding.snapshot.task_type == "真人类"


async def test_service_reports_an_unconfigured_portrait_source(tmp_path) -> None:
    service, store = await _production_service(
        tmp_path,
        bitable=_MixedCategoryBitable(),
        sources={"animation": _category_sources()["animation"]},
    )
    try:
        with pytest.raises(RunValidationError, match="真人类视图尚未配置"):
            await service.scan("portrait")
    finally:
        await store.close()


async def test_service_allows_approval_without_maker_for_animation(tmp_path) -> None:
    from feishu_generation_agent.storage.production_tasks import ProductionTaskStore

    class Bitable:
        async def ensure_schema(self, location): return object()
        async def list_tasks(self, location, schema, *, include_completed): return [_task()]

    class Runtime:
        async def start_run(self, request: RequirementRequest, *, run_id=None, thread_id=None):
            return run_id

    store = await ProductionTaskStore.open(tmp_path / "production.sqlite3")
    service = ProductionBitableService(
        bitable=Bitable(), store=store, runtime=Runtime(),
        sources={"animation": ProductionTaskSource(_location(), "动画类")},
        include_completed_for_test=True,
    )
    try:
        run_id = await service.claim("rec-no-maker")
        await service.validate_approval(run_id)
    finally:
        await store.close()


async def test_service_lists_active_production_run_for_browser_restore(tmp_path) -> None:
    from feishu_generation_agent.storage.production_tasks import ProductionTaskStore

    class Bitable:
        async def ensure_schema(self, location): return object()
        async def list_tasks(self, location, schema, *, include_completed): return [_task()]

    class Runtime:
        async def start_run(self, request, *, run_id=None, thread_id=None): return run_id

    store = await ProductionTaskStore.open(tmp_path / "production.sqlite3")
    service = ProductionBitableService(
        bitable=Bitable(), store=store, runtime=Runtime(),
        sources={"animation": ProductionTaskSource(_location(), "动画类")},
        include_completed_for_test=True,
    )
    try:
        run_id = await service.claim("rec-no-maker")
        active = await service.active_runs()
        scanned_after_claim = await service.scan()
    finally:
        await store.close()

    assert [(item.run_id, item.status.value) for item in active] == [
        (run_id, "处理中")
    ]
    assert scanned_after_claim == []


async def test_service_rerun_archives_original_binding_and_lists_it_as_recent(tmp_path) -> None:
    from feishu_generation_agent.domain.bitable import TableTaskStatus
    from feishu_generation_agent.storage.production_tasks import ProductionTaskStore

    class Bitable:
        async def ensure_schema(self, location): return object()
        async def list_tasks(self, location, schema, *, include_completed): return [_task()]

    class Runtime:
        def __init__(self) -> None:
            self.clone_calls: list[tuple[str, str, str]] = []

        async def start_run(self, request, *, run_id=None, thread_id=None): return run_id

        async def clone_run_for_approval(
            self, source_run_id, request, *, run_id, thread_id
        ):
            self.clone_calls.append((source_run_id, run_id, thread_id))
            return run_id

    store = await ProductionTaskStore.open(tmp_path / "production.sqlite3")
    runtime = Runtime()
    service = ProductionBitableService(
        bitable=Bitable(), store=store, runtime=runtime,
        sources={"animation": ProductionTaskSource(_location(), "动画类")},
        include_completed_for_test=True,
    )
    try:
        original_run_id = await service.claim("rec-no-maker")
        await store.release(original_run_id, status=TableTaskStatus.COMPLETED)

        rerun_id = await service.rerun(original_run_id)
        original = await store.get_by_run(original_run_id)
        recent = await service.recent_runs()
    finally:
        await store.close()

    assert rerun_id != original_run_id
    assert original is not None
    assert original.status is TableTaskStatus.COMPLETED
    assert [item.run_id for item in recent] == [original_run_id]
    assert len(runtime.clone_calls) == 1
    cloned_from, cloned_run_id, cloned_thread_id = runtime.clone_calls[0]
    assert cloned_from == original_run_id
    assert cloned_run_id == rerun_id
    assert cloned_thread_id != original.thread_id


async def test_service_rejects_rerun_of_non_animation_task(tmp_path) -> None:
    from feishu_generation_agent.domain.bitable import TableTaskStatus
    from feishu_generation_agent.storage.production_tasks import ProductionTaskStore

    class Bitable:
        async def ensure_schema(self, location): return object()
        async def list_tasks(self, location, schema, *, include_completed): return [_task()]

    class Runtime:
        async def start_run(self, request, *, run_id=None, thread_id=None): return run_id

    task = _task().model_copy(
        update={
            "task_type": "真人类",
            "snapshot": _task().snapshot.model_copy(update={"task_type": "真人类"}),
        }
    )
    store = await ProductionTaskStore.open(tmp_path / "production.sqlite3")
    service = ProductionBitableService(
        bitable=Bitable(), store=store, runtime=Runtime(),
        sources={"animation": ProductionTaskSource(_location(), "动画类")},
        include_completed_for_test=False,
    )
    try:
        binding = await store.claim(
            _location(), task, run_id="run-live-action", thread_id="thread-live-action"
        )
        await store.release(binding.run_id, status=TableTaskStatus.COMPLETED)
        with pytest.raises(RunConflict, match="真人类任务暂未启用"):
            await service.rerun(binding.run_id)
    finally:
        await store.close()
