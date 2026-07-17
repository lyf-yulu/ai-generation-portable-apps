import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from langgraph.types import Command

from feishu_generation_agent.config import Settings
from feishu_generation_agent.domain.document import (
    MediaAsset,
    NormalizedDocument,
    RequirementRequest,
)
from feishu_generation_agent.domain.errors import AgentError
from feishu_generation_agent.domain.plan import (
    ApprovalDecision,
    GenerationTask,
    ImageReference,
    TaskPlan,
)
from feishu_generation_agent.integrations.planner import validate_plan
from feishu_generation_agent.storage.files import FileStore
from feishu_generation_agent.storage.repository import Repository


class RunNotFound(LookupError):
    pass


class RunConflict(RuntimeError):
    pass


class RunValidationError(ValueError):
    pass


class GraphRuntime:
    def __init__(
        self,
        *,
        graph: Any,
        repository: Repository,
        file_store: FileStore,
        settings: Settings,
    ) -> None:
        self.graph = graph
        self.repository = repository
        self.file_store = file_store
        self.settings = settings
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._run_locks: dict[str, asyncio.Lock] = {}
        self._closed = False

    async def start_run(self, request: RequirementRequest) -> str:
        if self._closed:
            raise RunConflict("运行时正在关闭")
        run_id = str(uuid4())
        thread_id = str(uuid4())
        await self.repository.create_run(
            run_id,
            thread_id,
            request.source_url,
            status="created",
        )
        task = asyncio.create_task(
            self._run_to_approval(run_id, thread_id, request),
            name=f"approval-run-{run_id}",
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return run_id

    async def _run_to_approval(
        self,
        run_id: str,
        thread_id: str,
        request: RequirementRequest,
    ) -> None:
        try:
            await self.repository.update_run_status(run_id, "running")
            result = await self.graph.ainvoke(
                {
                    "run_id": run_id,
                    "thread_id": thread_id,
                    "source_url": request.source_url,
                    "requester_open_id": request.requester_open_id,
                    "trigger_type": request.trigger_type,
                    "reply_context": request.reply_context,
                    "status": "created",
                },
                config=self._config(thread_id),
            )
            status = (
                "waiting_approval"
                if self._has_interrupt(result)
                else self._safe_status(result.get("status"), "completed")
            )
            await self.repository.update_run_status(run_id, status)
        except asyncio.CancelledError:
            raise
        except Exception:
            await self.repository.append_event(
                run_id,
                "runtime",
                "failed",
                "Workflow background execution failed",
            )
            await self.repository.update_run_status(run_id, "failed")

    async def get_run_view(self, run_id: str) -> dict[str, Any]:
        run = await self.repository.get_run(run_id)
        if run is None:
            raise RunNotFound("运行不存在")
        snapshot = await self.graph.aget_state(self._config(run["thread_id"]))
        state = dict(snapshot.values or {})
        events = await self.repository.list_events(run_id)
        repository_status = self._safe_status(run.get("status"), "created")
        if repository_status in {"created", "running", "resuming", "failed"}:
            status = repository_status
        else:
            status = self._safe_status(state.get("status"), repository_status)
        plan = state.get("draft_plan")
        if plan is None:
            plan = state.get("task_plan")
        if not isinstance(plan, dict):
            plan = {"tasks": [], "document_summary": ""}
        tasks = plan.get("tasks")
        if not isinstance(tasks, list):
            tasks = []
        media_assets = self._safe_media_assets(
            run_id, state.get("media_assets", [])
        )
        view = {
            "run_id": run_id,
            "thread_id": run["thread_id"],
            "source_url": run["source_url"],
            "status": status,
            "created_at": run["created_at"],
            "updated_at": run["updated_at"],
            "events": self._event_view(events),
            "interrupt": self._interrupt_view(snapshot, state),
            "approval": {
                "document_id": state.get("document_id"),
                "document_title": state.get("document_title"),
                "document_revision": state.get(
                    "document_revision", state.get("source_revision")
                ),
                "revision": state.get(
                    "draft_revision",
                    state.get("document_revision", state.get("source_revision")),
                ),
                "tasks": tasks,
                "document_summary": plan.get("document_summary", ""),
                "media_assets": media_assets,
                "vision_descriptions": state.get("vision_descriptions", []),
                "validation_issues": state.get("validation_issues", []),
                "selected_task_ids": [
                    task.get("task_id")
                    for task in state.get("approved_tasks", [])
                    if isinstance(task, dict)
                ],
            },
        }
        return view

    async def resume_run(
        self,
        run_id: str,
        decision: ApprovalDecision,
    ) -> None:
        lock = self._run_locks.setdefault(run_id, asyncio.Lock())
        if lock.locked():
            raise RunConflict("审批正在处理中，请勿重复提交")
        async with lock:
            run = await self.repository.get_run(run_id)
            if run is None:
                raise RunNotFound("运行不存在")
            if run["status"] != "waiting_approval":
                raise RunConflict("只有等待审批的运行可以提交决定")
            snapshot = await self.graph.aget_state(
                self._config(run["thread_id"])
            )
            state = dict(snapshot.values or {})
            self._validate_decision(state, decision)
            await self.repository.update_run_status(run_id, "resuming")
            try:
                result = await self.graph.ainvoke(
                    Command(resume=decision.model_dump(mode="json")),
                    config=self._config(run["thread_id"]),
                )
            except AgentError as exc:
                await self.repository.update_run_status(run_id, "failed")
                raise RunValidationError(exc.detail.message) from None
            except Exception:
                await self.repository.append_event(
                    run_id,
                    "runtime",
                    "failed",
                    "Workflow approval resume failed",
                )
                await self.repository.update_run_status(run_id, "failed")
                raise RunConflict("审批恢复失败") from None
            status = (
                "waiting_approval"
                if self._has_interrupt(result)
                else self._safe_status(result.get("status"), "completed")
            )
            await self.repository.update_run_status(run_id, status)

    async def add_reference(
        self,
        run_id: str,
        *,
        task_id: str,
        role: str,
        order: int,
        filename: str,
        content: bytes,
        replaces_asset_id: str | None = None,
    ) -> dict[str, Any]:
        lock = self._run_locks.setdefault(run_id, asyncio.Lock())
        if lock.locked():
            raise RunConflict("运行正在更新，请稍后重试")
        async with lock:
            run, state = await self._waiting_state(run_id)
            plan = self._state_plan(state)
            task_index, task = self._task(plan, task_id)
            references = list(task.reference_images)
            if replaces_asset_id is not None:
                replace_index = next(
                    (
                        index
                        for index, reference in enumerate(references)
                        if reference.asset_id == replaces_asset_id
                    ),
                    None,
                )
                if replace_index is None:
                    raise RunValidationError(
                        f"替换素材 {replaces_asset_id} 不属于任务 {task_id}"
                    )
            else:
                replace_index = None

            try:
                verified = self.file_store.validate(content)
            except (TypeError, ValueError):
                raise RunValidationError("上传内容不是有效图片") from None
            if not verified.mime_type.startswith("image/"):
                raise RunValidationError("只允许上传真实图片")
            try:
                stored = self.file_store.save_input(run_id, filename, content)
            except (TypeError, ValueError):
                raise RunValidationError("图片保存失败") from None
            asset_id = f"upload-{uuid4().hex}"
            asset = MediaAsset(
                asset_id=asset_id,
                source_block_id=f"local-upload:{asset_id}",
                origin="local_upload",
                file_token=None,
                local_path=stored.local_path,
                mime_type=stored.mime_type,
                size=stored.size,
                sha256=stored.sha256,
                width=stored.width,
                height=stored.height,
            )
            reference = ImageReference(
                asset_id=asset_id,
                role=role,
                order=order,
            )
            if replace_index is None:
                references.append(reference)
            else:
                references[replace_index] = reference
            assets = [
                MediaAsset.model_validate(item)
                for item in state.get("media_assets", [])
            ]
            assets.append(asset)
            updated_task = self._task_with_references(task, references, assets)
            updated_plan = self._replace_task(plan, task_index, updated_task)
            await self._persist_draft(run, state, updated_plan, assets)
            return {
                "asset_id": asset_id,
                "mime_type": stored.mime_type,
                "size": stored.size,
                "width": stored.width,
                "height": stored.height,
            }

    async def set_references(
        self,
        run_id: str,
        *,
        task_id: str,
        references: list[ImageReference],
    ) -> None:
        lock = self._run_locks.setdefault(run_id, asyncio.Lock())
        if lock.locked():
            raise RunConflict("运行正在更新，请稍后重试")
        async with lock:
            run, state = await self._waiting_state(run_id)
            plan = self._state_plan(state)
            task_index, task = self._task(plan, task_id)
            assets = [
                MediaAsset.model_validate(item)
                for item in state.get("media_assets", [])
            ]
            updated_task = self._task_with_references(task, references, assets)
            updated_plan = self._replace_task(plan, task_index, updated_task)
            await self._persist_draft(run, state, updated_plan, assets)

    async def unlink_reference(
        self,
        run_id: str,
        *,
        task_id: str,
        asset_id: str,
    ) -> None:
        lock = self._run_locks.setdefault(run_id, asyncio.Lock())
        if lock.locked():
            raise RunConflict("运行正在更新，请稍后重试")
        async with lock:
            run, state = await self._waiting_state(run_id)
            plan = self._state_plan(state)
            task_index, task = self._task(plan, task_id)
            references = [
                reference
                for reference in task.reference_images
                if reference.asset_id != asset_id
            ]
            if len(references) == len(task.reference_images):
                raise RunValidationError(
                    f"素材 {asset_id} 不属于任务 {task_id}"
                )
            assets = [
                MediaAsset.model_validate(item)
                for item in state.get("media_assets", [])
            ]
            updated_task = self._task_with_references(task, references, assets)
            updated_plan = self._replace_task(plan, task_index, updated_task)
            await self._persist_draft(run, state, updated_plan, assets)

    async def get_reference_file(
        self,
        run_id: str,
        asset_id: str,
    ) -> tuple[Path, str]:
        run = await self.repository.get_run(run_id)
        if run is None:
            raise RunNotFound("运行不存在")
        snapshot = await self.graph.aget_state(self._config(run["thread_id"]))
        for item in snapshot.values.get("media_assets", []):
            if not isinstance(item, dict) or item.get("asset_id") != asset_id:
                continue
            try:
                asset = MediaAsset.model_validate(item)
            except Exception:
                break
            resolved_path = asset.local_path.resolve()
            data_root = self.settings.data_dir.resolve()
            if (
                not asset.mime_type.startswith("image/")
                or not resolved_path.is_relative_to(data_root)
                or not resolved_path.is_file()
            ):
                break
            return resolved_path, asset.mime_type
        raise RunNotFound("图片素材不存在")

    async def _waiting_state(
        self,
        run_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        run = await self.repository.get_run(run_id)
        if run is None:
            raise RunNotFound("运行不存在")
        if run["status"] != "waiting_approval":
            raise RunConflict("只有等待审批的运行可以修改素材")
        snapshot = await self.graph.aget_state(self._config(run["thread_id"]))
        state = dict(snapshot.values or {})
        if state.get("status") != "waiting_approval":
            raise RunConflict("当前 checkpoint 不在等待审批状态")
        return run, state

    @staticmethod
    def _state_plan(state: dict[str, Any]) -> TaskPlan:
        try:
            return TaskPlan.model_validate(
                state.get("draft_plan") or state.get("task_plan")
            )
        except Exception:
            raise RunValidationError("当前审批计划无效") from None

    @staticmethod
    def _task(plan: TaskPlan, task_id: str) -> tuple[int, GenerationTask]:
        for index, task in enumerate(plan.tasks):
            if task.task_id == task_id:
                return index, task
        raise RunValidationError(f"未知任务：{task_id}")

    def _task_with_references(
        self,
        task: GenerationTask,
        references: list[ImageReference],
        assets: list[MediaAsset],
    ) -> GenerationTask:
        try:
            updated = GenerationTask.model_validate(
                task.model_dump(mode="json")
                | {
                    "reference_images": [
                        reference.model_dump(mode="json")
                        for reference in references
                    ]
                }
            )
            self._validate_references(
                updated.task_type.value,
                updated.reference_images,
                {asset.asset_id for asset in assets},
            )
            return updated
        except RunValidationError:
            raise
        except Exception as exc:
            message = str(exc)
            if "reference_images" in message:
                raise RunValidationError("任务必须保留至少一张参考图片") from None
            raise RunValidationError("图片引用无效") from None

    @staticmethod
    def _replace_task(
        plan: TaskPlan,
        task_index: int,
        task: GenerationTask,
    ) -> TaskPlan:
        tasks = list(plan.tasks)
        tasks[task_index] = task
        return TaskPlan(tasks=tasks, document_summary=plan.document_summary)

    async def _persist_draft(
        self,
        run: dict[str, Any],
        state: dict[str, Any],
        plan: TaskPlan,
        assets: list[MediaAsset],
    ) -> None:
        plan_json = plan.model_dump(mode="json")
        asset_json = [asset.model_dump(mode="json") for asset in assets]
        normalized = self._document_assets(state.get("normalized_document"), asset_json)
        source_document = self._document_assets(state.get("source_document"), asset_json)
        validation_issues: list[str] = []
        if normalized is not None:
            try:
                validation_issues = validate_plan(
                    plan,
                    NormalizedDocument.model_validate(normalized),
                    max_output_count=self.settings.max_output_count,
                )
            except Exception:
                raise RunValidationError("更新后的任务计划无法验证") from None
        revision = state.get("draft_revision")
        if not isinstance(revision, int):
            revision = state.get("document_revision", state.get("source_revision", 0))
        updates: dict[str, Any] = {
            "draft_plan": plan_json,
            "task_plan": plan_json,
            "media_assets": asset_json,
            "draft_revision": revision + 1,
            "approval_decision": None,
            "approved_tasks": [],
            "validation_issues": validation_issues,
            "status": "waiting_approval",
        }
        if normalized is not None:
            updates["normalized_document"] = normalized
        if source_document is not None:
            updates["source_document"] = source_document
        config = self._config(run["thread_id"])
        try:
            await self.graph.aupdate_state(
                config,
                updates,
                as_node="validate_plan",
            )
            result = await self.graph.ainvoke(None, config=config)
        except Exception:
            raise RunConflict("更新审批 checkpoint 失败") from None
        if not self._has_interrupt(result):
            raise RunConflict("更新后未恢复到等待审批状态")
        await self.repository.update_run_status(run["run_id"], "waiting_approval")

    @staticmethod
    def _document_assets(value: Any, assets: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        updated = dict(value)
        updated["media_assets"] = assets
        return updated

    def _validate_decision(
        self,
        state: dict[str, Any],
        decision: ApprovalDecision,
    ) -> None:
        if decision.action != "approve":
            return
        raw_plan = state.get("draft_plan") or state.get("task_plan")
        try:
            original = TaskPlan.model_validate(raw_plan)
            candidate = (
                TaskPlan(
                    tasks=decision.tasks,
                    document_summary=original.document_summary,
                )
                if decision.tasks
                else original
            )
            original_ids = {task.task_id for task in original.tasks}
            if any(task.task_id not in original_ids for task in candidate.tasks):
                raise ValueError("编辑结果包含未知任务")
            assets = {
                item.get("asset_id")
                for item in state.get("media_assets", [])
                if isinstance(item, dict) and isinstance(item.get("asset_id"), str)
            }
            for task in candidate.tasks:
                self._validate_references(task.task_type.value, task.reference_images, assets)
            approved = candidate.approved_subset(
                decision.selected_task_ids,
                self.settings.max_output_count,
            )
            if not approved.tasks:
                raise ValueError("没有可批准的任务")
        except Exception as exc:
            message = str(exc)
            if "unknown selected task_id" in message:
                missing = message.rsplit(":", 1)[-1].strip()
                raise RunValidationError(f"未知任务：{missing}") from None
            raise RunValidationError("审批任务无效") from None

    @staticmethod
    def _validate_references(
        task_type: str,
        references: Any,
        known_assets: set[str],
    ) -> None:
        asset_ids = [reference.asset_id for reference in references]
        if any(asset_id not in known_assets for asset_id in asset_ids):
            raise RunValidationError("编辑结果引用了未知素材")
        if len(asset_ids) != len(set(asset_ids)):
            raise RunValidationError("同一任务不能重复引用同一图片")
        orders = [reference.order for reference in references]
        if len(orders) != len(set(orders)):
            raise RunValidationError("同一任务的图片顺序不能重复")
        roles = [reference.role for reference in references]
        allowed_roles = {"reference_image", "first_frame", "last_frame"}
        if any(role not in allowed_roles for role in roles):
            raise RunValidationError("图片用途无效")
        if roles.count("first_frame") > 1 or roles.count("last_frame") > 1:
            raise RunValidationError("首帧或尾帧用途不能重复")
        frame_roles = {"first_frame", "last_frame"}.intersection(roles)
        if "reference_image" in roles and frame_roles:
            raise RunValidationError("普通参考图不能与首尾帧混用")
        if task_type == "image_to_image" and frame_roles:
            raise RunValidationError("图生图只接受普通参考图")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        tasks = tuple(self._background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._background_tasks.clear()

    @staticmethod
    def _config(thread_id: str) -> dict[str, dict[str, str]]:
        return {"configurable": {"thread_id": thread_id}}

    @staticmethod
    def _has_interrupt(result: dict[str, Any]) -> bool:
        interrupts = result.get("__interrupt__")
        return isinstance(interrupts, (list, tuple)) and bool(interrupts)

    @staticmethod
    def _safe_status(value: Any, fallback: str) -> str:
        return value if isinstance(value, str) and value else fallback

    @staticmethod
    def _safe_media_assets(run_id: str, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        assets: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            asset_id = item.get("asset_id")
            if not isinstance(asset_id, str):
                continue
            assets.append(
                {
                    "asset_id": asset_id,
                    "origin": item.get("origin"),
                    "mime_type": item.get("mime_type"),
                    "size": item.get("size"),
                    "width": item.get("width"),
                    "height": item.get("height"),
                    "preview_url": (
                        f"/api/runs/{run_id}/references/{asset_id}/content"
                    ),
                }
            )
        return assets

    @staticmethod
    def _event_view(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        started: dict[str, datetime] = {}
        result: list[dict[str, Any]] = []
        for event in events:
            created_at = event.get("created_at")
            timestamp: datetime | None = None
            if isinstance(created_at, str):
                try:
                    timestamp = datetime.fromisoformat(created_at).astimezone(UTC)
                except ValueError:
                    timestamp = None
            node = event.get("node")
            status = event.get("status")
            if isinstance(node, str) and status == "started" and timestamp:
                started[node] = timestamp
            duration_ms: int | None = None
            if (
                isinstance(node, str)
                and status in {"completed", "failed"}
                and timestamp
                and node in started
            ):
                duration_ms = max(
                    0, int((timestamp - started.pop(node)).total_seconds() * 1000)
                )
            result.append(
                {
                    "node": node,
                    "status": status,
                    "summary": event.get("summary", ""),
                    "created_at": created_at,
                    "duration_ms": duration_ms,
                }
            )
        return result

    @staticmethod
    def _interrupt_view(snapshot: Any, state: dict[str, Any]) -> dict[str, str] | None:
        for task in getattr(snapshot, "tasks", ()):
            for pending in getattr(task, "interrupts", ()):
                value = getattr(pending, "value", None)
                if isinstance(value, dict) and value.get("action") == "review_plan":
                    return {
                        "action": "review_plan",
                        "status": GraphRuntime._safe_status(
                            state.get("status"), "waiting_approval"
                        ),
                    }
        return None
