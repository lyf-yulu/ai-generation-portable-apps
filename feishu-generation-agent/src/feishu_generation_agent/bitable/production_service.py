from typing import Any
from uuid import uuid4

from feishu_generation_agent.domain.bitable import BitableLocation
from feishu_generation_agent.domain.document import RequirementRequest
from feishu_generation_agent.graph.runtime import RunConflict, RunValidationError
from feishu_generation_agent.storage.production_tasks import ProductionTaskStore


class ProductionBitableService:
    def __init__(
        self,
        *,
        bitable: Any,
        store: ProductionTaskStore,
        runtime: Any,
        location: BitableLocation,
        include_completed_for_test: bool,
    ) -> None:
        self._bitable = bitable
        self._store = store
        self._runtime = runtime
        self._location = location
        self._include_completed_for_test = include_completed_for_test
        self._schema: Any | None = None

    async def scan(self):
        schema = await self._prepared_schema()
        return await self._bitable.list_tasks(
            self._location,
            schema,
            include_completed=self._include_completed_for_test,
        )

    async def claim(self, record_id: str) -> str:
        task = next((item for item in await self.scan() if item.record_id == record_id), None)
        if task is None:
            raise RunConflict("该生产表记录当前不可领取")
        binding = await self._store.claim(
            self._location,
            task,
            run_id=str(uuid4()),
            thread_id=str(uuid4()),
        )
        return await self._runtime.start_run(
            RequirementRequest(source_url=binding.source_url, trigger_type="production_bitable"),
            run_id=binding.run_id,
            thread_id=binding.thread_id,
        )

    async def validate_approval(self, run_id: str) -> None:
        binding = await self._store.get_by_run(run_id)
        if binding is not None and binding.maker_open_id is None:
            raise RunValidationError("缺少需求制作人；请先在生产表补齐后再批准")

    async def _prepared_schema(self):
        if self._schema is None:
            self._schema = await self._bitable.ensure_schema(self._location)
        return self._schema
