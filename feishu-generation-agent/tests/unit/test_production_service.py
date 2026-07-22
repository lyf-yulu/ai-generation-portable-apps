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
        source_url="https://tenant.feishu.cn/docx/docA", progress="未开始",
        snapshot=ProductionSourceSnapshot(
            requirement_name="需求 A", requirement_attachment="https://tenant.feishu.cn/docx/docA"
        ),
    )


async def test_service_allows_planning_but_blocks_approval_without_maker(tmp_path) -> None:
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
        with pytest.raises(RunValidationError, match="缺少需求制作人"):
            await service.validate_approval(run_id)
    finally:
        await store.close()
