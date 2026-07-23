from typing import Any

from feishu_generation_agent.domain.artifact import Artifact, DeliveryRecord
from feishu_generation_agent.domain.document import NormalizedDocument
from feishu_generation_agent.domain.plan import TaskPlan
from feishu_generation_agent.domain.production_bitable import ResultTableTarget
from feishu_generation_agent.integrations.bitable_delivery import BitableResultWriter
from feishu_generation_agent.storage.production_tasks import ProductionTaskStore
from feishu_generation_agent.storage.repository import Repository


_RESULT_FIELDS = (
    ("需求类型", 3),
    ("需求附件", 1),
    ("项目名称", 4),
    ("发起人", 11),
    ("需求制作人", 11),
    ("结果", 17),
)
_SHARED_RESULT_TARGET = "__shared_production_result__"
_SHARED_RESULT_NAME = "统一结果表"
_DELIVERY_TASK_ID = "__production_delivery__"
_CONTEXT_OPERATION = "production_delivery_context"


class ProductionResultWriter:
    def __init__(
        self,
        *,
        client: Any,
        store: ProductionTaskStore,
        repository: Repository,
        result_folder_token: str,
    ) -> None:
        self._client = client
        self._store = store
        self._repository = repository
        self._result_folder_token = result_folder_token

    async def deliver(self, run_id: str, document: NormalizedDocument, plan: TaskPlan, artifacts: list[Artifact]) -> DeliveryRecord:
        if not artifacts:
            raise ValueError("没有可写入结果表的生成产物")
        binding = await self._store.get_by_run(run_id)
        if binding is None:
            raise ValueError("生产表运行不存在")
        await self._save_context_if_absent(run_id, document, plan)
        target = await self._ensure_target()
        tokens = [
            await self._upload(run_id, target.app_token, artifact)
            for artifact in artifacts
        ]
        delivery = await self._store.reserve_delivery(run_id)
        fields = _result_fields(binding.snapshot, tokens)
        if delivery.result_record_id:
            await self._client.update_bitable_record(target.app_token, target.table_id, delivery.result_record_id, fields)
            record_id = delivery.result_record_id
        else:
            record_id = await self._client.create_bitable_record(target.app_token, target.table_id, fields)
            await self._store.complete_delivery(run_id, result_record_id=record_id)
        return DeliveryRecord(
            status="succeeded",
            target_type="production_result_record",
            app_token=target.app_token,
            table_id=target.table_id,
            record_id=record_id,
            result_table_url=target.url,
            uploaded_artifact_ids=[artifact.artifact_id for artifact in artifacts],
        )

    async def retry_delivery(self, run_id: str) -> DeliveryRecord:
        context = await self._repository.get_operation(
            run_id, _DELIVERY_TASK_ID, _CONTEXT_OPERATION
        )
        if context is None:
            raise ValueError("production delivery context does not exist")
        payload = context["payload"]
        document = NormalizedDocument.model_validate(payload.get("document"))
        plan = TaskPlan.model_validate(payload.get("plan"))
        artifacts = await self._repository.list_artifacts(run_id)
        return await self.deliver(run_id, document, plan, artifacts)

    async def _save_context_if_absent(
        self, run_id: str, document: NormalizedDocument, plan: TaskPlan
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
            None,
            "ready",
            {
                "document": document.model_dump(mode="json"),
                "plan": plan.model_dump(mode="json"),
            },
        )

    async def _ensure_target(self) -> ResultTableTarget:
        existing = await self._store.get_result_target(_SHARED_RESULT_TARGET)
        if existing is not None:
            return existing
        created = await self._client.create_bitable_app(_SHARED_RESULT_NAME, self._result_folder_token)
        fields = await self._client.list_bitable_fields(created.app_token, created.table_id)
        primary = next((item for item in fields if item.get("type") == 1), None)
        if not isinstance(primary, dict) or not isinstance(primary.get("field_id"), str):
            raise ValueError("结果表缺少可重命名的主字段")
        await self._client.update_bitable_field(created.app_token, created.table_id, primary["field_id"], "需求名称", 1)
        for name, field_type in _RESULT_FIELDS:
            await self._client.create_bitable_field(created.app_token, created.table_id, name, field_type)
        target = ResultTableTarget(maker_open_id=_SHARED_RESULT_TARGET, maker_name=_SHARED_RESULT_NAME, app_token=created.app_token, table_id=created.table_id, url=created.url)
        await self._store.upsert_result_target(target)
        return target

    async def _upload(self, run_id: str, app_token: str, artifact: Artifact) -> str:
        if artifact.feishu_file_token:
            return artifact.feishu_file_token
        token = await self._client.upload_media_all(
            artifact.local_path.name,
            BitableResultWriter._read_verified_artifact(artifact),
            artifact.mime_type,
            parent_type="bitable_file",
            parent_node=app_token,
        )
        await self._repository.save_artifact(
            run_id, artifact.model_copy(update={"feishu_file_token": token})
        )
        return token


def _result_fields(snapshot, file_tokens: list[str]) -> dict[str, object]:
    return {
        "需求名称": snapshot.requirement_name,
        "需求类型": snapshot.task_type,
        "需求附件": snapshot.requirement_attachment,
        "项目名称": snapshot.project_names,
        "发起人": [{"id": value} for value in snapshot.requester_open_ids],
        "需求制作人": [{"id": value} for value in snapshot.maker_open_ids],
        "结果": [{"file_token": token} for token in file_tokens],
    }
