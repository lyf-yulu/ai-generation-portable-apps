from typing import Any
from uuid import uuid4

from feishu_generation_agent.domain.bitable import BitableLocation, TableTaskStatus
from feishu_generation_agent.domain.document import RequirementRequest
from feishu_generation_agent.domain.production_bitable import ProductionTaskSummary
from feishu_generation_agent.graph.runtime import RunConflict, RunValidationError
from feishu_generation_agent.storage.production_tasks import ProductionTaskStore


_RELEASED_STATUSES = {
    "succeeded": TableTaskStatus.COMPLETED,
    "completed_with_errors": TableTaskStatus.COMPLETED,
    "failed": TableTaskStatus.FAILED,
    "cancelled": TableTaskStatus.FAILED,
}
_ACTIVE_STATUSES = {
    "created": TableTaskStatus.PROCESSING,
    "running": TableTaskStatus.PROCESSING,
    "waiting_approval": TableTaskStatus.WAITING_APPROVAL,
    "resuming": TableTaskStatus.GENERATING,
    "waiting_provider": TableTaskStatus.GENERATING,
    "delivering": TableTaskStatus.WRITING_BACK,
    "delivery_failed": TableTaskStatus.WRITEBACK_FAILED,
}
_SHARED_RESULT_TARGET = "__shared_production_result__"


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
        self._closed = False

    async def scan(self):
        schema = await self._prepared_schema()
        tasks = await self._bitable.list_tasks(
            self._location,
            schema,
            include_completed=self._include_completed_for_test,
        )
        location = await self._prepared_location()
        active_record_ids = {
            binding.record_id
            for binding in await self._store.list_active(
                location.app_token or "", location.table_id
            )
        }
        return [task for task in tasks if task.record_id not in active_record_ids]

    async def claim(self, record_id: str) -> str:
        task = next((item for item in await self.scan() if item.record_id == record_id), None)
        if task is None:
            raise RunConflict("该生产表记录当前不可领取")
        if task.task_type != "动画类":
            raise RunConflict(f"{task.task_type or '未分类'}任务暂未启用")
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

    async def active_runs(self):
        location = await self._prepared_location()
        return await self._store.list_active(location.app_token or "", location.table_id)

    async def recent_runs(self):
        location = await self._prepared_location()
        return await self._store.list_recent(location.app_token or "", location.table_id)

    async def result_table_url(self, run_id: str) -> str | None:
        binding = await self._store.get_by_run(run_id)
        if binding is None:
            return None
        target = await self._store.get_result_target(_SHARED_RESULT_TARGET)
        return target.url if target is not None else None

    async def rerun(self, run_id: str) -> str:
        source = await self._store.get_by_run(run_id)
        if source is None or source.status not in {
            TableTaskStatus.COMPLETED,
            TableTaskStatus.FAILED,
        }:
            raise RunConflict("只有已经结束的多维表格任务可以重跑")
        task = ProductionTaskSummary(
            record_id=source.record_id,
            display_text=source.display_text,
            source_url=source.source_url,
            progress=source.progress,
            maker_open_id=source.maker_open_id,
            maker_name=source.maker_name,
            snapshot=source.snapshot,
        )
        rerun = await self._store.claim(
            self._location,
            task,
            run_id=str(uuid4()),
            thread_id=str(uuid4()),
        )
        try:
            return await self._runtime.clone_run_for_approval(
                run_id,
                RequirementRequest(
                    source_url=rerun.source_url, trigger_type="production_bitable"
                ),
                run_id=rerun.run_id,
                thread_id=rerun.thread_id,
            )
        except Exception:
            await self._store.release(
                rerun.run_id,
                status=TableTaskStatus.FAILED,
                last_error="重跑初始化失败",
            )
            raise

    async def sync_once(self, run_id: str):
        binding = await self._store.get_by_run(run_id)
        if binding is None:
            from feishu_generation_agent.graph.runtime import RunNotFound
            raise RunNotFound("多维表格运行不存在")
        view = await self._runtime.get_run_view(run_id)
        runtime_status = view.get("status")
        if not isinstance(runtime_status, str):
            raise RunConflict("运行状态无效")
        released = _RELEASED_STATUSES.get(runtime_status)
        if released is not None:
            return await self._store.release(run_id, status=released)
        active = _ACTIVE_STATUSES.get(runtime_status)
        if active is None:
            raise RunConflict(f"无法同步运行状态：{runtime_status}")
        return await self._store.set_status(run_id, active)

    async def retry_delivery(self, run_id: str) -> None:
        binding = await self._store.get_by_run(run_id)
        if binding is None or binding.status is not TableTaskStatus.WRITEBACK_FAILED:
            raise RunConflict("只有交付失败的运行可以重试交付")
        await self._runtime.retry_delivery(run_id)
        await self._store.set_status(run_id, TableTaskStatus.WRITING_BACK)

    async def delete_run(self, run_id: str) -> None:
        binding = await self._store.get_by_run(run_id)
        await self._runtime.delete_run(run_id)
        if binding is not None and binding.status not in {
            TableTaskStatus.COMPLETED,
            TableTaskStatus.FAILED,
        }:
            await self._store.release(
                run_id, status=TableTaskStatus.FAILED, last_error="本地运行已删除"
            )

    async def resume_incomplete(self) -> list[str]:
        bindings = await self.active_runs()
        for binding in bindings:
            await self._runtime.start_run(
                RequirementRequest(
                    source_url=binding.source_url, trigger_type="production_bitable"
                ),
                run_id=binding.run_id,
                thread_id=binding.thread_id,
            )
        await self._runtime.resume_pending_runs()
        return [binding.run_id for binding in bindings]

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            await self._store.close()

    async def validate_approval(self, run_id: str) -> None:
        binding = await self._store.get_by_run(run_id)
        if binding is not None and binding.snapshot.task_type != "动画类":
            raise RunValidationError(f"{binding.snapshot.task_type or '未分类'}任务暂未启用")

    async def _prepared_schema(self):
        if self._closed:
            raise RunConflict("多维表格服务正在关闭")
        await self._prepared_location()
        if self._schema is None:
            self._schema = await self._bitable.ensure_schema(self._location)
        return self._schema

    async def _prepared_location(self) -> BitableLocation:
        if self._location.app_token is None:
            self._location = await self._bitable.resolve_location(self._location)
        return self._location
