from hashlib import sha256

from feishu_generation_agent.domain.artifact import Artifact
from feishu_generation_agent.domain.bitable import BitableLocation
from feishu_generation_agent.domain.document import NormalizedDocument, SourceType
from feishu_generation_agent.domain.plan import TaskPlan
from feishu_generation_agent.domain.production_bitable import (
    ProductionSourceSnapshot,
    ProductionTaskSummary,
)
from feishu_generation_agent.integrations.feishu_client import CreatedBitableApp
from feishu_generation_agent.integrations.production_delivery import ProductionResultWriter
from feishu_generation_agent.integrations.production_routing import (
    ProductionRoutingDeliveryWriter,
)
from feishu_generation_agent.storage.production_tasks import ProductionTaskStore
from feishu_generation_agent.storage.repository import Repository


async def test_delivery_creates_one_shared_table_and_updates_same_result_row(tmp_path) -> None:
    content = b"video"
    artifact_path = tmp_path / "result.mp4"
    artifact_path.write_bytes(content)
    artifact = Artifact(
        artifact_id="artifact-1", task_id="task-1", kind="video", local_path=artifact_path,
        mime_type="video/mp4", size=len(content), sha256=sha256(content).hexdigest(), status="succeeded",
    )
    store = await ProductionTaskStore.open(tmp_path / "production.sqlite3")
    repository = await Repository.open(tmp_path / "repository.sqlite3")
    await repository.create_run("run-1", "thread-1", "https://tenant.feishu.cn/docx/docA")
    await repository.save_artifact("run-1", artifact)
    location = BitableLocation(wiki_token="wiki", app_token="app-source", table_id="tbl-source", view_id="vew", source_url="https://tenant.feishu.cn/wiki/wiki?table=tbl-source&view=vew")
    task = ProductionTaskSummary(record_id="rec-source", display_text="需求 A", source_url="https://tenant.feishu.cn/docx/docA", progress="未开始", task_type="动画类", maker_open_id="ou-maker", maker_name="制作人", snapshot=ProductionSourceSnapshot(requirement_name="需求 A", task_type="动画类", requirement_attachment="https://tenant.feishu.cn/docx/docA", project_names=["项目"], requester_open_ids=["ou-request"], requester_names=["发起人"], maker_open_ids=["ou-maker"], maker_names=["制作人"]))
    await store.claim(location, task, run_id="run-1", thread_id="thread-1")

    class Client:
        created_apps = 0
        created_records = 0
        updated_records = 0
        async def create_bitable_app(self, name, folder_token):
            self.created_apps += 1
            return CreatedBitableApp("app-result", "tbl-result", "https://tenant.feishu.cn/base/app-result")
        async def list_bitable_fields(self, app_token, table_id): return [{"field_id": "fld-primary", "field_name": "Name", "type": 1}]
        async def update_bitable_field(self, app_token, table_id, field_id, field_name, field_type): return None
        async def create_bitable_field(self, app_token, table_id, field_name, field_type): return f"fld-{field_name}"
        async def grant_bitable_editor(self, app_token, open_id): return None
        async def upload_media_all(self, filename, content, mime_type, *, parent_type, parent_node): return "file-result"
        async def create_bitable_record(self, app_token, table_id, fields):
            self.created_records += 1
            assert list(fields) == ["需求名称", "需求类型", "需求附件", "项目名称", "发起人", "需求制作人", "结果"]
            return "rec-result"
        async def update_bitable_record(self, app_token, table_id, record_id, fields): self.updated_records += 1

    client = Client()
    writer = ProductionResultWriter(
        client=client,
        store=store,
        repository=repository,
        result_folder_token="fld-results",
    )
    document = NormalizedDocument(document_id="docA", title="需求 A", revision=1, source_type=SourceType.DOCX, source_token="docA", blocks=[], text_view="", media_assets=[])
    plan = TaskPlan(tasks=[])
    try:
        first = await writer.deliver("run-1", document, plan, [artifact])
        second = await writer.retry_delivery("run-1")
    finally:
        await store.close()
        await repository.close()

    assert first.record_id == second.record_id == "rec-result"
    assert first.target_type == second.target_type == "production_result_record"
    assert first.result_table_url == second.result_table_url == "https://tenant.feishu.cn/base/app-result"
    assert client.created_apps == 1
    assert client.created_records == 1
    assert client.updated_records == 1


async def test_production_routing_uses_result_writer_only_for_production_run(tmp_path) -> None:
    store = await ProductionTaskStore.open(tmp_path / "production.sqlite3")
    location = BitableLocation(
        wiki_token="wiki", app_token="app-source", table_id="tbl-source",
        view_id="vew", source_url="https://tenant.feishu.cn/wiki/wiki?table=tbl-source&view=vew",
    )
    task = ProductionTaskSummary(
        record_id="rec-source", display_text="需求 A", source_url="https://tenant.feishu.cn/docx/docA",
        progress="未开始", snapshot=ProductionSourceSnapshot(
            requirement_name="需求 A", requirement_attachment="https://tenant.feishu.cn/docx/docA",
        ),
    )
    await store.claim(location, task, run_id="production-run", thread_id="thread-1")

    class Writer:
        def __init__(self, result): self.result = result
        async def deliver(self, *args): return self.result
        async def retry_delivery(self, run_id): return self.result

    router = ProductionRoutingDeliveryWriter(
        store, production=Writer("production"), legacy=Writer("legacy")
    )
    try:
        assert await router.retry_delivery("production-run") == "production"
        assert await router.retry_delivery("legacy-run") == "legacy"
    finally:
        await store.close()
