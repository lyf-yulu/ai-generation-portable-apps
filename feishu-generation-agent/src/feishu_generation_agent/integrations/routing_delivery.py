from typing import Protocol

from feishu_generation_agent.domain import (
    Artifact,
    BitableBinding,
    DeliveryRecord,
    NormalizedDocument,
    TaskPlan,
)
from feishu_generation_agent.ports import DeliveryWriter


class BindingLookup(Protocol):
    async def get_by_run(self, run_id: str) -> BitableBinding | None: ...


class RoutingDeliveryWriter:
    def __init__(
        self,
        bindings: BindingLookup,
        *,
        bitable: DeliveryWriter,
        legacy: DeliveryWriter | None = None,
    ) -> None:
        self._bindings = bindings
        self._bitable = bitable
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
        if await self._bindings.get_by_run(run_id) is not None:
            return self._bitable
        if self._legacy is None:
            raise ValueError("legacy document delivery is not configured")
        return self._legacy
