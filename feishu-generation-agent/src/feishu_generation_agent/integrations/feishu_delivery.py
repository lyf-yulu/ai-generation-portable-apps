from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256
import os
from pathlib import Path
import stat
from typing import Protocol
from zoneinfo import ZoneInfo

from feishu_generation_agent.domain import (
    Artifact,
    DeliveryRecord,
    NormalizedDocument,
    TaskPlan,
)
from feishu_generation_agent.storage.repository import Repository


_DIRECT_UPLOAD_LIMIT = 20 * 1024 * 1024
_DELIVERY_TASK_ID = "__delivery__"
_BLOCK_BATCH_SIZE = 50


class DeliveryClient(Protocol):
    async def create_document(self, title: str) -> str: ...

    async def append_document_blocks(
        self, document_id: str, blocks: list[dict]
    ) -> None: ...

    async def upload_file_all(
        self, filename: str, content: bytes, mime_type: str
    ) -> str: ...

    async def prepare_file_upload(self, filename: str, size: int) -> tuple[str, int]: ...

    async def upload_file_part(
        self, upload_id: str, sequence: int, content: bytes
    ) -> None: ...

    async def finish_file_upload(self, upload_id: str, block_count: int) -> str: ...

    async def add_document_member(
        self, document_id: str, owner_open_id: str
    ) -> None: ...


class FeishuDeliveryWriter:
    def __init__(
        self,
        client: DeliveryClient,
        repository: Repository,
        *,
        owner_open_id: str,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not owner_open_id:
            raise ValueError("owner_open_id is required")
        self._client = client
        self._repository = repository
        self._owner_open_id = owner_open_id
        self._now = now or (lambda: datetime.now(UTC))

    async def deliver(
        self,
        run_id: str,
        document: NormalizedDocument,
        plan: TaskPlan,
        artifacts: list[Artifact],
    ) -> DeliveryRecord:
        for artifact in artifacts:
            self._read_verified_artifact(artifact, collect=False)
        context_operation = await self._repository.get_operation(
            run_id, _DELIVERY_TASK_ID, "delivery_context"
        )
        if context_operation is None:
            await self._repository.save_operation(
                run_id,
                _DELIVERY_TASK_ID,
                "delivery_context",
                None,
                "ready",
                {
                    "document": document.model_dump(mode="json"),
                    "plan": plan.model_dump(mode="json"),
                },
            )
        operation = await self._repository.get_operation(
            run_id, _DELIVERY_TASK_ID, "delivery_document"
        )
        if operation is None:
            local_time = self._now().astimezone(ZoneInfo("Asia/Shanghai"))
            title = f"[AI 交付] {document.title} - {local_time:%Y-%m-%d %H:%M}"
            document_id = await self._client.create_document(title)
            await self._repository.save_operation(
                run_id,
                _DELIVERY_TASK_ID,
                "delivery_document",
                document_id,
                "completed",
                {"title": title},
            )
        else:
            document_id = operation.get("official_id")
            if not isinstance(document_id, str) or not document_id:
                raise RuntimeError("delivery document identity is invalid")

        uploaded: list[Artifact] = []
        for artifact in artifacts:
            uploaded.append(await self._ensure_uploaded(run_id, artifact))

        blocks = self._build_blocks(document, plan, uploaded)
        for batch_index, offset in enumerate(
            range(0, len(blocks), _BLOCK_BATCH_SIZE)
        ):
            operation_name = f"delivery_blocks:{batch_index}"
            existing = await self._repository.get_operation(
                run_id, _DELIVERY_TASK_ID, operation_name
            )
            if existing is not None and existing["status"] == "completed":
                continue
            await self._client.append_document_blocks(
                document_id, blocks[offset : offset + _BLOCK_BATCH_SIZE]
            )
            await self._repository.save_operation(
                run_id,
                _DELIVERY_TASK_ID,
                operation_name,
                document_id,
                "completed",
                {"batch_index": batch_index},
            )

        permission = await self._repository.get_operation(
            run_id, _DELIVERY_TASK_ID, "delivery_permission"
        )
        if permission is None or permission["status"] != "completed":
            await self._client.add_document_member(
                document_id, self._owner_open_id
            )
            await self._repository.save_operation(
                run_id,
                _DELIVERY_TASK_ID,
                "delivery_permission",
                document_id,
                "completed",
                {"member_type": "openid", "permission": "edit"},
            )

        return DeliveryRecord(
            document_id=document_id,
            document_url=f"https://feishu.cn/docx/{document_id}",
            status="succeeded",
            uploaded_artifact_ids=[item.artifact_id for item in uploaded],
        )

    async def retry_delivery(self, run_id: str) -> DeliveryRecord:
        operation = await self._repository.get_operation(
            run_id, _DELIVERY_TASK_ID, "delivery_context"
        )
        if operation is None:
            raise ValueError("delivery context does not exist")
        payload = operation["payload"]
        document = NormalizedDocument.model_validate(payload.get("document"))
        plan = TaskPlan.model_validate(payload.get("plan"))
        artifacts = await self._repository.list_artifacts(run_id)
        return await self.deliver(run_id, document, plan, artifacts)

    async def _ensure_uploaded(
        self, run_id: str, artifact: Artifact
    ) -> Artifact:
        persisted = await self._repository.get_artifact(artifact.artifact_id)
        current = persisted or artifact
        if current.feishu_file_token:
            return current
        operation_name = f"upload:{artifact.artifact_id}"
        operation = await self._repository.get_operation(
            run_id, artifact.task_id, operation_name
        )
        if operation is not None and operation["status"] == "completed":
            token = operation.get("official_id")
            if isinstance(token, str) and token:
                updated = artifact.model_copy(update={"feishu_file_token": token})
                await self._repository.save_artifact(run_id, updated)
                return updated

        if artifact.size <= _DIRECT_UPLOAD_LIMIT:
            content = self._read_verified_artifact(artifact)
            token = await self._client.upload_file_all(
                artifact.local_path.name, content, artifact.mime_type
            )
        else:
            token = await self._upload_in_parts(run_id, artifact, operation)

        updated = artifact.model_copy(update={"feishu_file_token": token})
        await self._repository.save_artifact(run_id, updated)
        await self._repository.save_operation(
            run_id,
            artifact.task_id,
            operation_name,
            token,
            "completed",
            {"artifact_id": artifact.artifact_id},
        )
        return updated

    @staticmethod
    def _read_verified_artifact(
        artifact: Artifact, *, collect: bool = True
    ) -> bytes:
        descriptor = os.open(
            artifact.local_path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise ValueError("交付产物完整性校验失败")
            digest = sha256()
            chunks: list[bytes] = []
            size = 0
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                if collect:
                    chunks.append(chunk)
                digest.update(chunk)
                size += len(chunk)
            after = os.fstat(descriptor)
            current = artifact.local_path.lstat()
            if (
                size != artifact.size
                or digest.hexdigest() != artifact.sha256
                or (before.st_dev, before.st_ino, before.st_size)
                != (after.st_dev, after.st_ino, after.st_size)
                or (after.st_dev, after.st_ino)
                != (current.st_dev, current.st_ino)
            ):
                raise ValueError("交付产物完整性校验失败")
            return b"".join(chunks)
        finally:
            os.close(descriptor)

    async def _upload_in_parts(
        self,
        run_id: str,
        artifact: Artifact,
        operation: dict | None,
    ) -> str:
        operation_name = f"upload:{artifact.artifact_id}"
        payload = operation["payload"] if operation else {}
        upload_id = payload.get("upload_id")
        block_size = payload.get("block_size")
        completed_parts = set(payload.get("completed_parts", []))
        if not isinstance(upload_id, str) or not isinstance(block_size, int):
            upload_id, block_size = await self._client.prepare_file_upload(
                artifact.local_path.name, artifact.size
            )
            completed_parts = set()

        block_count = 0
        descriptor = os.open(
            artifact.local_path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        before = os.fstat(descriptor)
        digest = sha256()
        total_size = 0
        try:
            sequence = 0
            while True:
                content = os.read(descriptor, block_size)
                if not content:
                    break
                digest.update(content)
                total_size += len(content)
                block_count += 1
                if sequence not in completed_parts:
                    await self._client.upload_file_part(
                        upload_id, sequence, content
                    )
                    completed_parts.add(sequence)
                    await self._repository.save_operation(
                        run_id,
                        artifact.task_id,
                        operation_name,
                        upload_id,
                        "uploading",
                        {
                            "upload_id": upload_id,
                            "block_size": block_size,
                            "completed_parts": sorted(completed_parts),
                        },
                    )
                sequence += 1
            after = os.fstat(descriptor)
            current = artifact.local_path.lstat()
            if (
                total_size != artifact.size
                or digest.hexdigest() != artifact.sha256
                or (before.st_dev, before.st_ino, before.st_size)
                != (after.st_dev, after.st_ino, after.st_size)
                or (after.st_dev, after.st_ino)
                != (current.st_dev, current.st_ino)
            ):
                raise ValueError("交付产物完整性校验失败")
        finally:
            os.close(descriptor)
        return await self._client.finish_file_upload(upload_id, block_count)

    @staticmethod
    def _build_blocks(
        document: NormalizedDocument,
        plan: TaskPlan,
        artifacts: list[Artifact],
    ) -> list[dict]:
        blocks = [
            _text_block("heading1", "AI 生成交付"),
            _text_block(
                "text",
                f"原文档：{document.source_type.value}/{document.source_token}；"
                f"revision={document.revision}",
            ),
            _text_block("heading2", "执行摘要"),
            _text_block("text", plan.document_summary or "已完成所选生成任务。"),
        ]
        artifacts_by_task: dict[str, list[Artifact]] = {}
        for artifact in artifacts:
            artifacts_by_task.setdefault(artifact.task_id, []).append(artifact)
        for task in plan.tasks:
            blocks.extend(
                [
                    _text_block("heading2", task.title),
                    _text_block("text", f"最终提示词：{task.prompt}"),
                    _text_block(
                        "text",
                        f"参数：{task.task_type.value}，{task.aspect_ratio}，"
                        f"输出 {task.output_count} 个",
                    ),
                    _text_block(
                        "text",
                        "参考图：" + ", ".join(
                            f"{ref.order}:{ref.asset_id}({ref.role})"
                            for ref in task.reference_images
                        ),
                    ),
                ]
            )
            for artifact in artifacts_by_task.get(task.task_id, []):
                block_type = "image" if artifact.kind == "image" else "file"
                blocks.append(
                    {
                        "block_type": 27 if block_type == "image" else 23,
                        block_type: {"token": artifact.feishu_file_token},
                    }
                )
        failed_tasks = [
            task for task in plan.tasks if not artifacts_by_task.get(task.task_id)
        ]
        if failed_tasks:
            blocks.append(_text_block("heading2", "失败任务与重试建议"))
            for task in failed_tasks:
                blocks.append(
                    _text_block(
                        "text",
                        f"{task.title}：未生成可交付产物。请先核对供应商任务状态，"
                        "确认不会重复付费后再决定是否重试生成。",
                    )
                )
        return blocks


def _text_block(kind: str, text: str) -> dict:
    block_types = {"text": 2, "heading1": 3, "heading2": 4}
    return {
        "block_type": block_types[kind],
        kind: {"elements": [{"text_run": {"content": text}}]},
    }
