import pytest

from feishu_generation_agent.bitable.production_service import ProductionBitableService
from feishu_generation_agent.domain.bitable import BitableLocation
from feishu_generation_agent.domain.document import RequirementRequest
from feishu_generation_agent.domain.production_bitable import (
    ProductionSourceSnapshot,
    ProductionTaskSummary,
)
from feishu_generation_agent.graph.runtime import RunValidationError


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
        bitable=Bitable(), store=store, runtime=Runtime(), location=_location(),
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
        bitable=Bitable(), store=store, runtime=Runtime(), location=_location(),
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
        bitable=Bitable(), store=store, runtime=runtime, location=_location(),
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
