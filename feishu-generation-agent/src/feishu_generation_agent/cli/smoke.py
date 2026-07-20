import argparse
import asyncio
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import uuid4

from feishu_generation_agent.bootstrap import open_services
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
                    "resolution": "480p",
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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="显式门禁下执行一次真实图像与视频付费冒烟"
    )
    parser.add_argument(
        "--confirm-paid-smoke",
        action="store_true",
        help="第一道门禁；同时还必须设置 ALLOW_PAID_SMOKE=YES",
    )
    parser.add_argument("source_url", help="专用飞书测试文档 URL")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
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
