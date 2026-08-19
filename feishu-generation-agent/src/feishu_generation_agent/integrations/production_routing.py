from feishu_generation_agent.domain import (
    Artifact,
    DeliveryRecord,
    NormalizedDocument,
    TaskPlan,
)
from feishu_generation_agent.ports import DeliveryWriter
from feishu_generation_agent.storage.production_tasks import ProductionTaskStore


class ProductionRoutingDeliveryWriter:
    """Routes only source-production runs to their maker result tables."""

    def __init__(
        self,
        store: ProductionTaskStore,
        *,
        production: DeliveryWriter,
        legacy: DeliveryWriter | None = None,
    ) -> None:
        self._store = store
        self._production = production
        self._legacy = legacy

    async def deliver(
        self,
        run_id: str,
        document: NormalizedDocument,
        plan: TaskPlan,
        artifacts: list[Artifact],
    ) -> DeliveryRecord:
        writer = await self._writer_for(run_id)
        return await writer.deliver(run_id, document, plan, artifacts)

    async def retry_delivery(self, run_id: str) -> DeliveryRecord:
        writer = await self._writer_for(run_id)
        return await writer.retry_delivery(run_id)

    async def _writer_for(self, run_id: str) -> DeliveryWriter:
        if await self._store.get_by_run(run_id) is not None:
            return self._production
        if self._legacy is None:
            # 直连入口的 run 没有生产表绑定：legacy 未配置时进统一结果表兜底，
            # 否则这类 run 的产出无处可去（2026-08-18 线上交付失败根因）。
            return self._production
        return self._legacy
