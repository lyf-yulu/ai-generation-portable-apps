import asyncio
import hashlib
import json
from typing import Any, Protocol
from uuid import uuid4

from feishu_generation_agent.domain.bitable import (
    BitableBinding,
    BitableLocation,
    BitableTaskSummary,
    TableTaskStatus,
)
from feishu_generation_agent.domain.document import RequirementRequest
from feishu_generation_agent.graph.runtime import (
    GraphRuntime,
    RunConflict,
    RunNotFound,
)
from feishu_generation_agent.integrations.feishu_bitable import (
    BitableSchema,
    FeishuBitableClient,
)
from feishu_generation_agent.storage.bitable_tasks import BitableTaskStore


_CLAIMANT = "local-mvp"
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


class _Runtime(Protocol):
    async def start_run(
        self,
        request: RequirementRequest,
        *,
        run_id: str | None = None,
        thread_id: str | None = None,
    ) -> str: ...

    async def get_run_view(self, run_id: str) -> dict[str, Any]: ...

    async def retry_delivery(self, run_id: str) -> None: ...

    async def delete_run(self, run_id: str) -> None: ...

    async def resume_pending_runs(self) -> None: ...


class BitableMvpService:
    def __init__(
        self,
        *,
        bitable: FeishuBitableClient,
        store: BitableTaskStore,
        runtime: GraphRuntime,
        location: BitableLocation,
    ) -> None:
        self._bitable = bitable
        self._store = store
        self._runtime: _Runtime = runtime
        self._configured_location = location
        self._location: BitableLocation | None = None
        self._schema: BitableSchema | None = None
        self._prepare_lock = asyncio.Lock()
        self._closed = False

    async def prepare(self) -> BitableLocation:
        if self._closed:
            raise RunConflict("多维表格服务正在关闭")
        if self._location is not None:
            return self._location
        async with self._prepare_lock:
            if self._location is None:
                location = await self._bitable.resolve_location(
                    self._configured_location
                )
                schema = await self._bitable.ensure_schema(location)
                self._location = location
                self._schema = schema
        assert self._location is not None
        return self._location

    async def scan(self) -> list[BitableTaskSummary]:
        location, schema = await self._prepared()
        tasks = await self._bitable.list_tasks(location, schema)
        active = {
            binding.record_id
            for binding in await self._store.list_active(
                location.app_token or "",
                location.table_id,
            )
        }
        return [
            task
            for task in tasks
            if not task.has_result and task.record_id not in active
        ]

    async def active_runs(self) -> list[BitableBinding]:
        location, _ = await self._prepared()
        return await self._store.list_active(
            location.app_token or "",
            location.table_id,
        )

    async def claim(self, record_id: str) -> str:
        location, schema = await self._prepared()
        tasks = await self._bitable.list_tasks(location, schema)
        task = next(
            (
                item
                for item in tasks
                if item.record_id == record_id and not item.has_result
            ),
            None,
        )
        if task is None:
            raise RunConflict("该记录当前不可领取")
        run_id = str(uuid4())
        thread_id = str(uuid4())
        binding = await self._store.claim(
            app_token=location.app_token or "",
            table_id=location.table_id,
            view_id=location.view_id,
            record_id=task.record_id,
            source_url=task.source_url,
            display_text=task.display_text,
            claimant_open_id=_CLAIMANT,
            run_id=run_id,
            thread_id=thread_id,
            reply_context={},
        )
        request = RequirementRequest(
            source_url=binding.source_url,
            trigger_type="bitable",
            reply_context={},
        )
        await self._runtime.start_run(
            request,
            run_id=binding.run_id,
            thread_id=binding.thread_id,
        )
        return binding.run_id

    async def sync_once(self, run_id: str) -> BitableBinding:
        binding = await self._store.get_by_run(run_id)
        if binding is None:
            raise RunNotFound("多维表格运行不存在")
        view = await self._runtime.get_run_view(run_id)
        status = view.get("status")
        if not isinstance(status, str):
            raise RunConflict("运行状态无效")
        if status == "waiting_approval":
            fingerprint = _approval_fingerprint(view.get("approval"))
            binding, _ = await self._store.advance_approval(
                run_id, fingerprint
            )
        released = _RELEASED_STATUSES.get(status)
        if released is not None:
            return await self._store.release(run_id, status=released)
        active = _ACTIVE_STATUSES.get(status)
        if active is None:
            raise RunConflict(f"无法同步运行状态：{status}")
        return await self._store.set_status(run_id, active)

    async def retry_delivery(self, run_id: str) -> None:
        binding = await self._store.get_by_run(run_id)
        if (
            binding is None
            or binding.status is not TableTaskStatus.WRITEBACK_FAILED
        ):
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
                run_id,
                status=TableTaskStatus.FAILED,
                last_error="本地运行已删除",
            )

    async def resume_incomplete(self) -> list[str]:
        location, _ = await self._prepared()
        bindings = await self._store.list_active(
            location.app_token or "",
            location.table_id,
        )
        for binding in bindings:
            await self._runtime.start_run(
                RequirementRequest(
                    source_url=binding.source_url,
                    trigger_type="bitable",
                    reply_context={},
                ),
                run_id=binding.run_id,
                thread_id=binding.thread_id,
            )
        await self._runtime.resume_pending_runs()
        for binding in bindings:
            try:
                await self._runtime.get_run_view(binding.run_id)
            except (RunNotFound, KeyError):
                await self._runtime.start_run(
                    RequirementRequest(
                        source_url=binding.source_url,
                        trigger_type="bitable",
                        reply_context={},
                    ),
                    run_id=binding.run_id,
                    thread_id=binding.thread_id,
                )
                continue
            await self.sync_once(binding.run_id)
        return [binding.run_id for binding in bindings]

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._store.close()

    async def _prepared(self) -> tuple[BitableLocation, BitableSchema]:
        location = await self.prepare()
        assert self._schema is not None
        return location, self._schema


def _approval_fingerprint(value: Any) -> str:
    serialized = json.dumps(
        value if isinstance(value, dict) else {},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
