from hashlib import sha256
from pathlib import Path

import pytest

from feishu_generation_agent.domain import (
    Artifact,
    BitableBinding,
    GenerationTask,
    NormalizedDocument,
    SourceType,
    TableTaskStatus,
    TaskPlan,
)
from feishu_generation_agent.integrations.bitable_delivery import (
    BitableResultConflict,
    BitableResultWriter,
)
from feishu_generation_agent.integrations.routing_delivery import RoutingDeliveryWriter
from feishu_generation_agent.storage.repository import Repository


def _document() -> NormalizedDocument:
    return NormalizedDocument(
        document_id="source-doc",
        title="棋局",
        revision=7,
        source_type=SourceType.DOCX,
        source_token="source-doc",
        blocks=[],
        text_view="棋局需求",
        media_assets=[],
    )


def _plan() -> TaskPlan:
    return TaskPlan(
        document_summary="棋局生成",
        tasks=[
            GenerationTask(
                task_id="task-image",
                task_type="image_to_image",
                title="棋局图片",
                source_block_ids=["block-1"],
                user_intent="生成棋局",
                prompt="黑白棋子",
                reference_images=[
                    {"asset_id": "asset-1", "role": "reference_image", "order": 1}
                ],
                aspect_ratio="1:1",
                image_size="1024x1024",
                output_count=2,
            )
        ],
    )


def _artifact(path: Path, artifact_id: str) -> Artifact:
    content = path.read_bytes()
    return Artifact(
        artifact_id=artifact_id,
        task_id="task-image",
        kind="image",
        local_path=path,
        mime_type="image/png",
        size=len(content),
        sha256=sha256(content).hexdigest(),
        provider_task_id="provider-1",
        status="ready",
    )


def _binding(run_id: str = "run-bitable") -> BitableBinding:
    return BitableBinding(
        app_token="appTABLE",
        table_id="tblTABLE",
        view_id="vewTASKS",
        record_id="recTASK",
        source_url="https://tenant.feishu.cn/docx/docABC",
        display_text="棋局",
        run_id=run_id,
        thread_id="thread-bitable",
        claimant_open_id="local-mvp",
        status=TableTaskStatus.WRITING_BACK,
    )


class FakeBindingStore:
    def __init__(self, binding: BitableBinding | None) -> None:
        self.binding = binding

    async def get_by_run(self, run_id: str) -> BitableBinding | None:
        if self.binding is not None and self.binding.run_id == run_id:
            return self.binding
        return None


class FakeBitableDeliveryClient:
    def __init__(self) -> None:
        self.result: list[dict] = []
        self.reads = 0
        self.uploads: list[tuple[str, bytes, str, str]] = []
        self.updates: list[tuple[str, list[str]]] = []
        self.fail_update_once = False

    async def get_record(self, location, record_id: str) -> dict:
        self.reads += 1
        assert location.app_token == "appTABLE"
        assert record_id == "recTASK"
        return {"record_id": record_id, "fields": {"结果": list(self.result)}}

    async def upload_file_all(
        self,
        filename: str,
        content: bytes,
        mime_type: str,
        *,
        parent_type: str,
        parent_node: str,
    ) -> str:
        self.uploads.append((filename, content, parent_type, parent_node))
        assert mime_type == "image/png"
        return f"file-{len(self.uploads)}"

    async def prepare_file_upload(self, filename, size, *, parent_type, parent_node):
        raise AssertionError("small fixture must use direct upload")

    async def upload_file_part(self, upload_id, sequence, content):
        raise AssertionError("small fixture must use direct upload")

    async def finish_file_upload(self, upload_id, block_count):
        raise AssertionError("small fixture must use direct upload")

    async def write_result_attachments(
        self, location, record_id: str, file_tokens: list[str]
    ) -> dict:
        self.updates.append((record_id, list(file_tokens)))
        if self.fail_update_once:
            self.fail_update_once = False
            raise RuntimeError("fictional update failure")
        self.result = [{"file_token": token} for token in file_tokens]
        return {"record_id": record_id, "fields": {"结果": self.result}}


async def _artifacts(tmp_path: Path) -> list[Artifact]:
    artifacts = []
    for index, content in enumerate((b"first-image", b"second-image"), start=1):
        path = tmp_path / f"result-{index}.png"
        path.write_bytes(content)
        artifacts.append(_artifact(path, f"artifact-{index}"))
    return artifacts


async def test_bitable_delivery_uploads_every_artifact_once_and_writes_all_tokens(
    tmp_path: Path,
) -> None:
    repository = await Repository.open(tmp_path / "business.sqlite3")
    client = FakeBitableDeliveryClient()
    writer = BitableResultWriter(
        client, repository, FakeBindingStore(_binding())
    )
    artifacts = await _artifacts(tmp_path)

    record = await writer.deliver(
        "run-bitable", _document(), _plan(), artifacts
    )

    assert [upload[0] for upload in client.uploads] == [
        "result-1.png",
        "result-2.png",
    ]
    assert all(upload[2:] == ("bitable_file", "appTABLE") for upload in client.uploads)
    assert client.updates == [("recTASK", ["file-1", "file-2"])]
    assert client.reads == 2
    assert record.target_type == "bitable_record"
    assert record.app_token == "appTABLE"
    assert record.table_id == "tblTABLE"
    assert record.record_id == "recTASK"
    assert record.uploaded_artifact_ids == ["artifact-1", "artifact-2"]
    await repository.close()


async def test_bitable_delivery_retry_reuses_persisted_upload_tokens(
    tmp_path: Path,
) -> None:
    repository = await Repository.open(tmp_path / "business.sqlite3")
    client = FakeBitableDeliveryClient()
    client.fail_update_once = True
    writer = BitableResultWriter(
        client, repository, FakeBindingStore(_binding())
    )
    artifacts = await _artifacts(tmp_path)
    for artifact in artifacts:
        await repository.save_artifact("run-bitable", artifact)

    with pytest.raises(RuntimeError, match="update failure"):
        await writer.deliver("run-bitable", _document(), _plan(), artifacts)
    retried = await writer.retry_delivery("run-bitable")

    assert retried.status == "succeeded"
    assert len(client.uploads) == 2
    assert client.updates == [
        ("recTASK", ["file-1", "file-2"]),
        ("recTASK", ["file-1", "file-2"]),
    ]
    operations = await repository.list_operations("run-bitable")
    assert {
        item["operation"]
        for item in operations
        if item["operation"].startswith("bitable_upload:")
    } == {"bitable_upload:artifact-1", "bitable_upload:artifact-2"}
    await repository.close()


async def test_non_empty_result_conflicts_before_any_upload_or_update(
    tmp_path: Path,
) -> None:
    repository = await Repository.open(tmp_path / "business.sqlite3")
    client = FakeBitableDeliveryClient()
    client.result = [{"file_token": "someone-elses-result"}]
    writer = BitableResultWriter(
        client, repository, FakeBindingStore(_binding())
    )

    with pytest.raises(BitableResultConflict, match="结果"):
        await writer.deliver(
            "run-bitable", _document(), _plan(), await _artifacts(tmp_path)
        )

    assert client.reads == 1
    assert client.uploads == []
    assert client.updates == []
    await repository.close()


async def test_bitable_delivery_does_not_reuse_unscoped_legacy_file_token(
    tmp_path: Path,
) -> None:
    repository = await Repository.open(tmp_path / "business.sqlite3")
    client = FakeBitableDeliveryClient()
    writer = BitableResultWriter(
        client, repository, FakeBindingStore(_binding())
    )
    artifact = (await _artifacts(tmp_path))[0].model_copy(
        update={"feishu_file_token": "legacy-explorer-token"}
    )
    await repository.save_artifact("run-bitable", artifact)

    await writer.deliver("run-bitable", _document(), _plan(), [artifact])

    assert len(client.uploads) == 1
    assert client.updates == [("recTASK", ["file-1"])]
    await repository.close()


async def test_result_filled_during_upload_conflicts_before_update(
    tmp_path: Path,
) -> None:
    class ResultAppearsClient(FakeBitableDeliveryClient):
        async def get_record(self, location, record_id: str) -> dict:
            record = await super().get_record(location, record_id)
            if self.reads == 2:
                record["fields"]["结果"] = [{"file_token": "external-result"}]
            return record

    repository = await Repository.open(tmp_path / "business.sqlite3")
    client = ResultAppearsClient()
    writer = BitableResultWriter(
        client, repository, FakeBindingStore(_binding())
    )

    with pytest.raises(BitableResultConflict, match="结果"):
        await writer.deliver(
            "run-bitable", _document(), _plan(), await _artifacts(tmp_path)
        )

    assert len(client.uploads) == 2
    assert client.updates == []
    operations = await repository.list_operations("run-bitable")
    assert sum(
        item["status"] == "completed"
        for item in operations
        if item["operation"].startswith("bitable_upload:")
    ) == 2
    await repository.close()


async def test_routing_writer_uses_binding_to_select_bitable_or_legacy(
    tmp_path: Path,
) -> None:
    class FakeWriter:
        def __init__(self, label: str) -> None:
            self.label = label
            self.calls: list[str] = []

        async def deliver(self, run_id, document, plan, artifacts):
            self.calls.append(run_id)
            return self.label

        async def retry_delivery(self, run_id):
            self.calls.append(f"retry:{run_id}")
            return self.label

    bitable = FakeWriter("bitable")
    legacy = FakeWriter("legacy")
    store = FakeBindingStore(_binding())
    writer = RoutingDeliveryWriter(store, bitable=bitable, legacy=legacy)

    assert await writer.deliver("run-bitable", None, None, []) == "bitable"
    assert await writer.deliver("run-legacy", None, None, []) == "legacy"
    assert await writer.retry_delivery("run-bitable") == "bitable"
    assert await writer.retry_delivery("run-legacy") == "legacy"
    assert bitable.calls == ["run-bitable", "retry:run-bitable"]
    assert legacy.calls == ["run-legacy", "retry:run-legacy"]
