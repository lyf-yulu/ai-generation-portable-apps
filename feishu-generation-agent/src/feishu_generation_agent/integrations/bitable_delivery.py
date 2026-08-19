from hashlib import sha256
import os
import stat
from typing import Protocol

from feishu_generation_agent.domain import (
    Artifact,
    BitableBinding,
    BitableLocation,
    DeliveryRecord,
    NormalizedDocument,
    TaskPlan,
)
from feishu_generation_agent.integrations.feishu_bitable import record_has_result
from feishu_generation_agent.storage.repository import Repository


_DIRECT_UPLOAD_LIMIT = 20 * 1024 * 1024
_DELIVERY_TASK_ID = "__delivery__"
_CONTEXT_OPERATION = "bitable_delivery_context"


class BitableResultConflict(RuntimeError):
    """Raised when the result field was filled by another actor."""


class BindingSource(Protocol):
    async def get_by_run(self, run_id: str) -> BitableBinding | None: ...


class BitableDeliveryClient(Protocol):
    async def get_record(self, location: BitableLocation, record_id: str) -> dict: ...

    async def write_result_attachments(
        self, location: BitableLocation, record_id: str, file_tokens: list[str]
    ) -> dict: ...

    async def upload_file_all(
        self,
        filename: str,
        content: bytes,
        mime_type: str,
        *,
        parent_type: str,
        parent_node: str,
    ) -> str: ...

    async def prepare_file_upload(
        self,
        filename: str,
        size: int,
        *,
        parent_type: str,
        parent_node: str,
    ) -> tuple[str, int]: ...

    async def upload_file_part(
        self, upload_id: str, sequence: int, content: bytes
    ) -> None: ...

    async def finish_file_upload(self, upload_id: str, block_count: int) -> str: ...


class BitableResultWriter:
    def __init__(
        self,
        client: BitableDeliveryClient,
        repository: Repository,
        bindings: BindingSource,
    ) -> None:
        self._client = client
        self._repository = repository
        self._bindings = bindings

    async def deliver(
        self,
        run_id: str,
        document: NormalizedDocument,
        plan: TaskPlan,
        artifacts: list[Artifact],
    ) -> DeliveryRecord:
        if not artifacts:
            raise ValueError("没有可写回的生成产物")
        for artifact in artifacts:
            self._read_verified_artifact(artifact, collect=False)

        binding = await self._require_binding(run_id)
        location = self._location(binding)
        await self._save_context_if_absent(run_id, binding, document, plan)
        await self._ensure_result_empty(location, binding.record_id)

        uploaded = [
            await self._ensure_uploaded(run_id, location, artifact)
            for artifact in artifacts
        ]
        tokens = [artifact.feishu_file_token for artifact in uploaded]
        if any(not isinstance(token, str) or not token for token in tokens):
            raise RuntimeError("多维表格附件上传 token 无效")

        # This refresh is deliberately adjacent to the update. Never overwrite.
        await self._ensure_result_empty(location, binding.record_id)
        await self._client.write_result_attachments(
            location, binding.record_id, [str(token) for token in tokens]
        )
        return DeliveryRecord(
            status="succeeded",
            target_type="bitable_record",
            app_token=binding.app_token,
            table_id=binding.table_id,
            record_id=binding.record_id,
            uploaded_artifact_ids=[artifact.artifact_id for artifact in uploaded],
        )

    async def retry_delivery(self, run_id: str) -> DeliveryRecord:
        context = await self._repository.get_operation(
            run_id, _DELIVERY_TASK_ID, _CONTEXT_OPERATION
        )
        if context is None:
            raise ValueError("bitable delivery context does not exist")
        payload = context["payload"]
        document = NormalizedDocument.model_validate(payload.get("document"))
        plan = TaskPlan.model_validate(payload.get("plan"))
        artifacts = await self._repository.list_artifacts(run_id)
        return await self.deliver(run_id, document, plan, artifacts)

    async def _require_binding(self, run_id: str) -> BitableBinding:
        binding = await self._bindings.get_by_run(run_id)
        if binding is None:
            raise ValueError("run is not bound to a Bitable record")
        return binding

    @staticmethod
    def _location(binding: BitableBinding) -> BitableLocation:
        return BitableLocation(
            wiki_token="resolved",
            app_token=binding.app_token,
            table_id=binding.table_id,
            view_id=binding.view_id,
            source_url=binding.source_url,
        )

    async def _save_context_if_absent(
        self,
        run_id: str,
        binding: BitableBinding,
        document: NormalizedDocument,
        plan: TaskPlan,
    ) -> None:
        existing = await self._repository.get_operation(
            run_id, _DELIVERY_TASK_ID, _CONTEXT_OPERATION
        )
        if existing is not None:
            return
        await self._repository.save_operation(
            run_id,
            _DELIVERY_TASK_ID,
            _CONTEXT_OPERATION,
            binding.record_id,
            "ready",
            {
                "document": document.model_dump(mode="json"),
                "plan": plan.model_dump(mode="json"),
                "target": {
                    "app_token": binding.app_token,
                    "table_id": binding.table_id,
                    "view_id": binding.view_id,
                    "record_id": binding.record_id,
                },
            },
        )

    async def _ensure_result_empty(
        self, location: BitableLocation, record_id: str
    ) -> None:
        record = await self._client.get_record(location, record_id)
        if record_has_result(record):
            raise BitableResultConflict("结果列已存在附件，已停止回写")

    async def _ensure_uploaded(
        self,
        run_id: str,
        location: BitableLocation,
        artifact: Artifact,
    ) -> Artifact:
        operation_name = f"bitable_upload:{artifact.artifact_id}"
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
            token = await self._client.upload_file_all(
                artifact.local_path.name,
                self._read_verified_artifact(artifact),
                artifact.mime_type,
                parent_type="bitable_file",
                parent_node=location.app_token or "",
            )
        else:
            token = await self._upload_in_parts(
                run_id, location, artifact, operation
            )

        await self._repository.save_operation(
            run_id,
            artifact.task_id,
            operation_name,
            token,
            "completed",
            {"artifact_id": artifact.artifact_id},
        )
        updated = artifact.model_copy(update={"feishu_file_token": token})
        await self._repository.save_artifact(run_id, updated)
        return updated

    async def _upload_in_parts(
        self,
        run_id: str,
        location: BitableLocation,
        artifact: Artifact,
        operation: dict | None,
    ) -> str:
        operation_name = f"bitable_upload:{artifact.artifact_id}"
        payload = operation["payload"] if operation else {}
        upload_id = payload.get("upload_id")
        block_size = payload.get("block_size")
        completed_parts = set(payload.get("completed_parts", []))
        if not isinstance(upload_id, str) or not isinstance(block_size, int):
            upload_id, block_size = await self._client.prepare_file_upload(
                artifact.local_path.name,
                artifact.size,
                parent_type="bitable_file",
                parent_node=location.app_token or "",
            )
            completed_parts = set()

        descriptor = os.open(
            artifact.local_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        )
        before = os.fstat(descriptor)
        digest = sha256()
        total_size = 0
        block_count = 0
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
                    await self._client.upload_file_part(upload_id, sequence, content)
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
            self._verify_open_file(artifact, descriptor, before, digest, total_size)
        finally:
            os.close(descriptor)
        return await self._client.finish_file_upload(upload_id, block_count)

    @staticmethod
    def _read_verified_artifact(
        artifact: Artifact, *, collect: bool = True
    ) -> bytes:
        descriptor = os.open(
            artifact.local_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        )
        before = os.fstat(descriptor)
        digest = sha256()
        chunks: list[bytes] = []
        size = 0
        try:
            if not stat.S_ISREG(before.st_mode):
                raise ValueError("交付产物完整性校验失败")
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
                if collect:
                    chunks.append(chunk)
            BitableResultWriter._verify_open_file(
                artifact, descriptor, before, digest, size
            )
            return b"".join(chunks)
        finally:
            os.close(descriptor)

    @staticmethod
    def _verify_open_file(
        artifact: Artifact,
        descriptor: int,
        before: os.stat_result,
        digest,
        size: int,
    ) -> None:
        after = os.fstat(descriptor)
        current = artifact.local_path.lstat()
        if (
            not stat.S_ISREG(after.st_mode)
            or size != artifact.size
            or digest.hexdigest() != artifact.sha256
            or (before.st_dev, before.st_ino, before.st_size)
            != (after.st_dev, after.st_ino, after.st_size)
            or (after.st_dev, after.st_ino) != (current.st_dev, current.st_ino)
        ):
            raise ValueError("交付产物完整性校验失败")
