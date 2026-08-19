import argparse
import asyncio
import os
import sys
from collections.abc import Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Protocol
from uuid import uuid4

from feishu_generation_agent.bootstrap import open_application_services, open_services
from feishu_generation_agent.bitable.mvp_service import BitableMvpService
from feishu_generation_agent.config import Settings
from feishu_generation_agent.domain import (
    Artifact,
    ExecutionRecord,
    RequirementRequest,
    TaskPlan,
    TaskType,
)
from feishu_generation_agent.graph.nodes import (
    _execute_one_task,
    _task_assets,
    verify_and_download_artifacts,
)
from feishu_generation_agent.graph.builder import build_graph
from feishu_generation_agent.graph.runtime import GraphRuntime
from feishu_generation_agent.integrations.bitable_url import parse_bitable_url
from feishu_generation_agent.integrations.feishu_bitable import FeishuBitableClient
from feishu_generation_agent.integrations.feishu_client import FeishuClient
from feishu_generation_agent.integrations.production_bitable import (
    ProductionBitableClient,
)
from feishu_generation_agent.integrations.feishu_source import FeishuDocumentSource
from feishu_generation_agent.storage.checkpoints import open_checkpointer
from feishu_generation_agent.storage.files import FileStore


_TERMINAL_STATUSES = frozenset(
    {
        "succeeded",
        "completed_with_errors",
        "delivery_failed",
        "failed",
        "cancelled",
    }
)


class _BitableSmokeService(Protocol):
    async def scan(self) -> list[Any]: ...

    async def claim(self, record_id: str) -> str: ...

    async def sync_once(self, run_id: str) -> Any: ...


class _SmokeRuntime(Protocol):
    async def get_run_view(self, run_id: str) -> dict[str, Any]: ...


@dataclass(slots=True)
class BitableApprovalSmokeRunner:
    """Runs one Bitable task only until the existing human approval gate."""

    bitable_service: _BitableSmokeService
    runtime: _SmokeRuntime
    record_id: str
    poll_interval_seconds: float = 0.2

    async def run(self) -> str:
        tasks = await self.bitable_service.scan()
        if not any(task.record_id == self.record_id for task in tasks):
            raise RuntimeError("指定记录不在当前可领取任务中")
        run_id = await self.bitable_service.claim(self.record_id)
        while True:
            await self.bitable_service.sync_once(run_id)
            view = await self.runtime.get_run_view(run_id)
            status = view.get("status")
            if status == "waiting_approval":
                return run_id
            if status in _TERMINAL_STATUSES:
                raise RuntimeError("运行未到审批门禁即已结束")
            await asyncio.sleep(self.poll_interval_seconds)


@dataclass(slots=True)
class BitableReadOnlySmokeRunner:
    """Verifies Bitable location, schema and view access without claiming work."""

    settings: Settings

    async def run(self) -> int:
        self.settings.require(
            "lark_app_id",
            "lark_app_secret",
            "lark_bitable_url",
            "lark_bitable_table_id",
            "lark_bitable_view_id",
        )
        client = FeishuClient(self.settings)
        try:
            bitable = FeishuBitableClient(client)
            location = parse_bitable_url(
                self.settings.lark_bitable_url or "",
                self.settings.lark_bitable_table_id or "",
                self.settings.lark_bitable_view_id or "",
            )
            location = await bitable.resolve_location(location)
            schema = await bitable.ensure_schema(location)
            return len(await bitable.list_tasks(location, schema))
        except Exception as exc:
            raise RuntimeError("多维表格只读扫描失败") from exc
        finally:
            await client.close()


@dataclass(slots=True)
class ProductionBitableReadOnlySmokeRunner:
    """Reads the production source and one requirement document without mutation."""

    settings: Settings

    async def run(self) -> int:
        self.settings.require(
            "lark_app_id",
            "lark_app_secret",
            "lark_production_bitable_url",
            "lark_production_table_id",
            "lark_production_view_id",
            "lark_result_folder_token",
        )
        self.settings.ensure_paths()
        client = FeishuClient(self.settings)
        file_store = FileStore(
            self.settings.data_dir,
            self.settings.outputs_dir,
            max_bytes=self.settings.max_download_bytes,
        )
        try:
            bitable = ProductionBitableClient(client)
            location = parse_bitable_url(
                self.settings.lark_production_bitable_url or "",
                self.settings.lark_production_table_id or "",
                self.settings.lark_production_view_id or "",
            )
            location = await bitable.resolve_location(location)
            schema = await bitable.ensure_schema(location)
            tasks = await bitable.list_tasks(
                location,
                schema,
                include_completed=self.settings.lark_include_completed_for_test,
            )
            if tasks:
                source = FeishuDocumentSource(client, file_store)
                await source.ingest(RequirementRequest(source_url=tasks[0].source_url))
            return len(tasks)
        except Exception as exc:
            raise RuntimeError("生产多维表格只读扫描失败") from exc
        finally:
            file_store.close()
            await client.close()


@asynccontextmanager
async def _open_bitable_approval_smoke(
    settings: Settings,
) -> AsyncIterator[tuple[BitableMvpService, GraphRuntime]]:
    async with open_application_services(settings) as application:
        if application.bitable_factory is None:
            raise RuntimeError("尚未配置多维表格")
        async with open_checkpointer(application.graph.settings) as checkpointer:
            runtime = GraphRuntime(
                graph=build_graph(application.graph, checkpointer),
                repository=application.graph.repository,
                file_store=application.graph.file_store,
                settings=application.graph.settings,
                delivery_writer=application.graph.delivery_writer,
            )
            bitable_service = application.bitable_factory.create(runtime)
            try:
                yield bitable_service, runtime
            finally:
                await bitable_service.close()
                await runtime.close()


@dataclass(slots=True)
class PaidSmokeRunner:
    settings: Settings
    source_url: str

    @staticmethod
    async def _confirm(label: str) -> None:
        answer = await asyncio.to_thread(
            input, f"即将执行：{label}。输入 YES 继续："
        )
        if answer != "YES":
            raise RuntimeError(f"用户未确认，已在 {label} 前停止")

    async def run(self) -> None:
        self.settings.require(
            "lark_app_id",
            "lark_app_secret",
            "lark_output_owner_open_id",
            "deepseek_api_key",
            "claude_api_key",
            "chiyun_api_key",
            "ark_api_key",
        )
        print(
            "预计会调用：Claude 图片理解、DeepSeek 规划/审查/退回重规划、"
            "Chiyun 生成 1 张图、Seedance 生成 1 个最短视频、飞书交付。"
        )
        run_id = f"smoke-{uuid4().hex}"
        thread_id = f"smoke-thread-{uuid4().hex}"
        selected_tasks = []
        async with open_services(self.settings) as services:
            document = await services.document_source.ingest(
                RequirementRequest(source_url=self.source_url)
            )
            if not document.media_assets:
                raise RuntimeError("测试文档没有可用于图生图/图生视频的图片")
            visions = []
            for index, asset in enumerate(document.media_assets[:2], start=1):
                await self._confirm(f"Claude 理解第 {index} 张图片")
                visions.append(await services.vision_analyzer.analyze(asset))
            await self._confirm("DeepSeek 首次规划")
            first_plan = await services.planner.plan(document, visions)
            await self._confirm("DeepSeek 独立审查")
            await services.planner.audit(document, first_plan)
            await self._confirm("模拟退回后由 DeepSeek 重新规划")
            revised = await services.planner.plan(
                document,
                visions,
                "冒烟验证退回：保持原需求含义，修正任何歧义并保留最低成本输出。",
            )
            await self._confirm("DeepSeek 审查重规划结果")
            audit = await services.planner.audit(document, revised)
            if audit.corrections_required:
                raise RuntimeError("重规划结果仍被审查判定需要修正，停止生成")

            image = next(
                (
                    task
                    for task in revised.tasks
                    if task.task_type is TaskType.IMAGE_TO_IMAGE
                    and not task.blocking_issues
                ),
                None,
            )
            video = next(
                (
                    task
                    for task in revised.tasks
                    if task.task_type is TaskType.IMAGE_TO_VIDEO
                    and not task.blocking_issues
                ),
                None,
            )
            if image is None or video is None:
                raise RuntimeError("重规划结果没有同时包含可执行的图生图和图生视频任务")
            image = image.model_copy(update={"output_count": 1})
            video = video.model_copy(
                update={
                    "output_count": 1,
                    "duration": 4,
                    "resolution": "720p",
                    "generate_audio": False,
                }
            )
            selected_tasks = [image, video]
            selected_plan = TaskPlan(
                tasks=selected_tasks,
                document_summary=revised.document_summary,
            )
            await services.repository.create_run(
                run_id, thread_id, self.source_url, status="running"
            )
            records: list[ExecutionRecord] = []
            artifacts: list[Artifact] = []
            for task, label in (
                (image, "Chiyun 生成 1 张图"),
                (video, "Seedance 生成 1 个 4 秒视频"),
            ):
                await self._confirm(label)
                record, task_artifacts = await _execute_one_task(
                    services,
                    run_id,
                    task,
                    _task_assets(task, document),
                )
                records.append(record)
                artifacts.extend(task_artifacts)
                if record.status != "succeeded":
                    raise RuntimeError(f"{label}未成功，状态为 {record.status}")

            state = {
                "run_id": run_id,
                "thread_id": thread_id,
                "normalized_document": document.model_dump(mode="json"),
                "draft_plan": selected_plan.model_dump(mode="json"),
                "approved_tasks": [
                    task.model_dump(mode="json") for task in selected_tasks
                ],
                "execution_records": [
                    record.model_dump(mode="json") for record in records
                ],
                "artifacts": [
                    artifact.model_dump(mode="json") for artifact in artifacts
                ],
                "status": "verification_pending",
            }
            verified = await verify_and_download_artifacts(
                state,
                {"configurable": {"thread_id": thread_id}},
                services=services,
            )
            artifacts = [
                Artifact.model_validate(item) for item in verified["artifacts"]
            ]
            await self._confirm("创建飞书交付文档并上传产物")
            delivery = await services.delivery_writer.deliver(
                run_id, document, selected_plan, artifacts
            )
            operation_count = await services.repository.count_operations()
            await services.repository.update_run_status(run_id, "succeeded")

        async with open_services(self.settings) as restarted:
            before = await restarted.repository.count_operations()
            if before != operation_count:
                raise RuntimeError("重启前后 operation 计数不一致")
            for task in selected_tasks:
                operation = await restarted.repository.get_operation(
                    run_id, task.task_id, "submit"
                )
                existing = await restarted.repository.list_artifacts(
                    run_id, task_id=task.task_id
                )
                if (
                    operation is None
                    or operation["phase"] != "succeeded"
                    or not existing
                    or not all(
                        restarted.file_store.verify_artifact(run_id, item)
                        for item in existing
                    )
                ):
                    raise RuntimeError("重启恢复前置校验失败，拒绝触发供应商调用")
                recovered_record, _ = await _execute_one_task(
                    restarted,
                    run_id,
                    task,
                    _task_assets(task, document),
                )
                if recovered_record.status != "succeeded":
                    raise RuntimeError("重启恢复未复用已完成任务")
            if await restarted.repository.count_operations() != operation_count:
                raise RuntimeError("重启恢复产生了新的供应商 operation")
        print(f"冒烟通过，交付文档：{delivery.document_url}")


def build_paid_smoke_runner(settings: Settings, source_url: str) -> PaidSmokeRunner:
    return PaidSmokeRunner(settings=settings, source_url=source_url)


def build_bitable_read_only_smoke_runner(
    settings: Settings,
) -> BitableReadOnlySmokeRunner:
    return BitableReadOnlySmokeRunner(settings=settings)


def build_production_bitable_read_only_smoke_runner(
    settings: Settings,
) -> ProductionBitableReadOnlySmokeRunner:
    return ProductionBitableReadOnlySmokeRunner(settings=settings)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="执行飞书多维表格只读或审批门禁冒烟；付费生成须显式确认"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--bitable-read-only",
        action="store_true",
        help="只读取多维表格字段和任务视图，不领取、不调用模型或生成器",
    )
    mode.add_argument(
        "--production-bitable-read-only",
        action="store_true",
        help="只读取生产表和一条需求附件，不锁定、不生成、不创建结果表",
    )
    mode.add_argument(
        "--bitable-record-id",
        help="领取一条任务并在 waiting_approval 门禁停止，不提交图像或视频生成",
    )
    parser.add_argument(
        "--confirm-paid-smoke",
        action="store_true",
        help="第一道门禁；同时还必须设置 ALLOW_PAID_SMOKE=YES",
    )
    parser.add_argument(
        "source_url",
        nargs="?",
        help="专用飞书测试文档 URL；仅真实付费冒烟模式需要",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.bitable_read_only:
        if args.source_url:
            print("只读扫描不接受文档 URL", file=sys.stderr)
            return 2
        try:
            count = asyncio.run(
                build_bitable_read_only_smoke_runner(Settings()).run()
            )
        except (ValueError, RuntimeError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"多维表格只读扫描通过：当前可处理记录 {count} 条")
        return 0
    if args.production_bitable_read_only:
        if args.source_url:
            print("生产表只读扫描不接受文档 URL", file=sys.stderr)
            return 2
        try:
            count = asyncio.run(
                build_production_bitable_read_only_smoke_runner(Settings()).run()
            )
        except (ValueError, RuntimeError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"生产多维表格只读扫描通过：当前可处理记录 {count} 条")
        return 0
    if args.bitable_record_id:
        if args.source_url:
            print("审批门禁冒烟不接受文档 URL", file=sys.stderr)
            return 2
        try:
            async def run_to_gate() -> str:
                async with _open_bitable_approval_smoke(
                    Settings()
                ) as (bitable_service, runtime):
                    runner = BitableApprovalSmokeRunner(
                        bitable_service=bitable_service,
                        runtime=runtime,
                        record_id=args.bitable_record_id,
                    )
                    return await runner.run()

            run_id = asyncio.run(run_to_gate())
        except (ValueError, RuntimeError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"已到人工审批门禁：{run_id}；未提交图像或视频生成")
        return 0
    if not args.source_url:
        print("请指定 --bitable-read-only、--bitable-record-id 或专用文档 URL", file=sys.stderr)
        return 2
    if not args.confirm_paid_smoke or os.environ.get("ALLOW_PAID_SMOKE") != "YES":
        print(
            "拒绝执行：必须同时传入 --confirm-paid-smoke 并设置 "
            "ALLOW_PAID_SMOKE=YES。",
            file=sys.stderr,
        )
        return 2
    runner = build_paid_smoke_runner(Settings(), args.source_url)
    try:
        asyncio.run(runner.run())
    except (ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
