from collections.abc import Mapping
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from feishu_generation_agent.domain.bitable import BitableLocation, TableTaskStatus
from feishu_generation_agent.domain.document import (
    PlanningPromptSnapshot,
    RequirementRequest,
    build_planning_prompt_snapshot,
)
from feishu_generation_agent.domain.production_bitable import ProductionTaskSummary
from feishu_generation_agent.graph.runtime import (
    RunConflict,
    RunNotFound,
    RunValidationError,
)
from feishu_generation_agent.integrations.planner import planner_system_prompt
from feishu_generation_agent.storage.production_tasks import ProductionTaskStore


_RELEASED_STATUSES = {
    "succeeded": TableTaskStatus.COMPLETED,
    "completed_with_errors": TableTaskStatus.FAILED,
    "failed": TableTaskStatus.FAILED,
    "cancelled": TableTaskStatus.FAILED,
    "timed_out": TableTaskStatus.FAILED,
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


@dataclass(frozen=True, slots=True)
class ProductionTaskSource:
    location: BitableLocation
    expected_task_type: str


class ProductionBitableService:
    def __init__(
        self,
        *,
        bitable: Any,
        store: ProductionTaskStore,
        runtime: Any,
        sources: Mapping[str, ProductionTaskSource],
        include_completed_for_test: bool,
        enabled_task_types: frozenset[str] = frozenset({"动画类"}),
    ) -> None:
        self._bitable = bitable
        self._store = store
        self._runtime = runtime
        self._sources = dict(sources)
        self._include_completed_for_test = include_completed_for_test
        self._enabled_task_types = enabled_task_types
        self._schemas: dict[tuple[str, str], Any] = {}
        self._closed = False

    async def scan(self, category: str = "animation"):
        source = await self._prepared_source(category)
        schema_key = (
            source.location.app_token or "",
            source.location.table_id,
        )
        schema = self._schemas.get(schema_key)
        if schema is None:
            schema = await self._bitable.ensure_schema(source.location)
            self._schemas[schema_key] = schema
        tasks = await self._bitable.list_tasks(
            source.location,
            schema,
            include_completed=self._include_completed_for_test,
        )
        active_record_ids = {
            binding.record_id
            for binding in await self._store.list_active(*schema_key)
        }
        return [
            task
            for task in tasks
            if task.task_type == source.expected_task_type
            and task.record_id not in active_record_ids
        ]

    async def claim(
        self,
        record_id: str,
        category: str = "animation",
        *,
        owner_user_id: str = "prime-local",
        planning_prompt: PlanningPromptSnapshot | None = None,
    ) -> str:
        source = await self._prepared_source(category)
        task = next(
            (item for item in await self.scan(category) if item.record_id == record_id),
            None,
        )
        if task is None:
            raise RunConflict("该生产表记录当前不可领取")
        if task.task_type not in self._enabled_task_types:
            raise RunConflict(f"{task.task_type or '未分类'}任务暂未启用")
        binding = await self._store.claim(
            source.location,
            task,
            run_id=str(uuid4()),
            thread_id=str(uuid4()),
            owner_user_id=owner_user_id,
        )
        if planning_prompt is None:
            planning_prompt = build_planning_prompt_snapshot(
                owner_user_id=owner_user_id,
                source="prime",
                version=0,
                prompt_text=planner_system_prompt(),
            )
        with self._runtime_owner_scope(owner_user_id):
            return await self._runtime.start_run(
                RequirementRequest(
                    source_url=binding.source_url,
                    trigger_type="production_bitable",
                    planning_prompt=planning_prompt,
                ),
                run_id=binding.run_id,
                thread_id=binding.thread_id,
            )

    async def active_runs(self, *, owner_user_id: str = "prime-local"):
        location = await self._table_location()
        return await self._store.list_active(
            location.app_token or "",
            location.table_id,
            owner_user_id=owner_user_id,
        )

    async def recent_runs(self, *, owner_user_id: str = "prime-local"):
        location = await self._table_location()
        return await self._store.list_recent(
            location.app_token or "",
            location.table_id,
            owner_user_id=owner_user_id,
        )

    async def result_table_url(
        self, run_id: str, *, owner_user_id: str = "prime-local"
    ) -> str | None:
        binding = await self._store.get_by_run(
            run_id, owner_user_id=owner_user_id
        )
        if binding is None:
            return None
        target = await self._store.get_result_target(_SHARED_RESULT_TARGET)
        return target.url if target is not None else None

    async def is_production_run(
        self, run_id: str, *, owner_user_id: str = "prime-local"
    ) -> bool:
        state = await self._store.run_owner_state(
            run_id, owner_user_id=owner_user_id
        )
        if state == "other":
            raise RunNotFound("多维表格运行不存在")
        return state == "owned"

    async def rerun(
        self, run_id: str, *, owner_user_id: str = "prime-local"
    ) -> str:
        source = await self._store.get_by_run(
            run_id, owner_user_id=owner_user_id
        )
        if source is None:
            raise RunNotFound("多维表格运行不存在")
        if source.status not in {
            TableTaskStatus.COMPLETED,
            TableTaskStatus.FAILED,
        }:
            raise RunConflict("只有已经结束的多维表格任务可以重跑")
        if source.snapshot.task_type not in self._enabled_task_types:
            raise RunConflict(f"{source.snapshot.task_type or '未分类'}任务暂未启用")
        task = ProductionTaskSummary(
            record_id=source.record_id,
            display_text=source.display_text,
            source_url=source.source_url,
            progress=source.progress,
            task_type=source.snapshot.task_type,
            maker_open_id=source.maker_open_id,
            maker_name=source.maker_name,
            snapshot=source.snapshot,
        )
        rerun = await self._store.claim(
            source.source_location,
            task,
            run_id=str(uuid4()),
            thread_id=str(uuid4()),
            owner_user_id=owner_user_id,
        )
        try:
            with self._runtime_owner_scope(owner_user_id):
                return await self._runtime.clone_run_for_approval(
                    run_id,
                    RequirementRequest(
                        source_url=rerun.source_url,
                        trigger_type="production_bitable",
                    ),
                    run_id=rerun.run_id,
                    thread_id=rerun.thread_id,
                )
        except Exception:
            await self._store.release(
                rerun.run_id,
                status=TableTaskStatus.FAILED,
                last_error="重跑初始化失败",
                owner_user_id=owner_user_id,
            )
            raise

    async def sync_once(
        self, run_id: str, *, owner_user_id: str | None = None
    ):
        binding = await self._store.get_by_run(
            run_id, owner_user_id=owner_user_id
        )
        if binding is None:
            raise RunNotFound("多维表格运行不存在")
        with self._runtime_owner_scope(binding.owner_user_id):
            view = await self._runtime.get_run_view(run_id)
        runtime_status = view.get("status")
        if not isinstance(runtime_status, str):
            raise RunConflict("运行状态无效")
        released = _RELEASED_STATUSES.get(runtime_status)
        if released is not None:
            return await self._store.release(
                run_id,
                status=released,
                owner_user_id=binding.owner_user_id,
            )
        active = _ACTIVE_STATUSES.get(runtime_status)
        if active is None:
            raise RunConflict(f"无法同步运行状态：{runtime_status}")
        return await self._store.set_status(
            run_id, active, owner_user_id=binding.owner_user_id
        )

    async def retry_delivery(
        self, run_id: str, *, owner_user_id: str = "prime-local"
    ) -> None:
        binding = await self._store.get_by_run(
            run_id, owner_user_id=owner_user_id
        )
        if binding is None:
            raise RunNotFound("多维表格运行不存在")
        if binding.status is not TableTaskStatus.WRITEBACK_FAILED:
            raise RunConflict("只有交付失败的运行可以重试交付")
        with self._runtime_owner_scope(owner_user_id):
            await self._runtime.retry_delivery(run_id)
        await self._store.set_status(
            run_id,
            TableTaskStatus.WRITING_BACK,
            owner_user_id=owner_user_id,
        )

    async def delete_run(
        self, run_id: str, *, owner_user_id: str = "prime-local"
    ) -> None:
        binding = await self._store.get_by_run(
            run_id, owner_user_id=owner_user_id
        )
        if binding is None:
            raise RunNotFound("多维表格运行不存在")
        with self._runtime_owner_scope(owner_user_id):
            await self._runtime.delete_run(run_id)
        if binding.status not in {
            TableTaskStatus.COMPLETED,
            TableTaskStatus.FAILED,
        }:
            await self._store.release(
                run_id,
                status=TableTaskStatus.FAILED,
                last_error="本地运行已删除",
                owner_user_id=owner_user_id,
            )

    async def resume_incomplete(self) -> list[str]:
        location = await self._table_location()
        # Startup recovery is intentionally cross-owner and never exposed by
        # a user-facing route.
        bindings = await self._store.list_active(
            location.app_token or "", location.table_id
        )
        for binding in bindings:
            with self._runtime_owner_scope(binding.owner_user_id):
                await self._runtime.start_run(
                    RequirementRequest(
                        source_url=binding.source_url,
                        trigger_type="production_bitable",
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

    async def validate_approval(
        self, run_id: str, *, owner_user_id: str = "prime-local"
    ) -> None:
        binding = await self._store.get_by_run(
            run_id, owner_user_id=owner_user_id
        )
        if binding is None:
            raise RunNotFound("多维表格运行不存在")
        if binding.snapshot.task_type not in self._enabled_task_types:
            raise RunValidationError(f"{binding.snapshot.task_type or '未分类'}任务暂未启用")

    async def _prepared_source(self, category: str) -> ProductionTaskSource:
        if self._closed:
            raise RunConflict("多维表格服务正在关闭")
        source = self._sources.get(category)
        if source is None:
            label = "真人类" if category == "portrait" else "动画类"
            raise RunValidationError(f"{label}视图尚未配置")
        if source.location.app_token is None:
            resolved = await self._bitable.resolve_location(source.location)
            source = ProductionTaskSource(resolved, source.expected_task_type)
            self._sources[category] = source
        return source

    async def _table_location(self) -> BitableLocation:
        return (await self._prepared_source("animation")).location

    def _runtime_owner_scope(self, owner_user_id: str):
        repository = getattr(self._runtime, "repository", None)
        owner_scope = getattr(repository, "owner_scope", None)
        if callable(owner_scope):
            return owner_scope(owner_user_id)
        return nullcontext()
