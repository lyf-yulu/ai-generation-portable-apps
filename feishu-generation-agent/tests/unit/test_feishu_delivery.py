from datetime import UTC, datetime
from hashlib import sha256
import json
from pathlib import Path

import httpx
import pytest

from feishu_generation_agent.config import Settings
from feishu_generation_agent.domain import (
    Artifact,
    GenerationTask,
    NormalizedDocument,
    SourceType,
    TaskPlan,
)
from feishu_generation_agent.integrations.feishu_delivery import (
    FeishuDeliveryWriter,
)
from feishu_generation_agent.integrations.feishu_client import FeishuClient
from feishu_generation_agent.storage.repository import Repository


class FakeDeliveryClient:
    def __init__(self) -> None:
        self.created_titles: list[str] = []
        self.block_batches: list[list[dict]] = []
        self.direct_uploads: list[tuple[str, bytes]] = []
        self.prepares: list[tuple[str, int]] = []
        self.parts: list[tuple[str, int, bytes]] = []
        self.finishes: list[tuple[str, int]] = []
        self.members: list[tuple[str, str]] = []

    async def create_document(self, title: str) -> str:
        self.created_titles.append(title)
        return "delivery-doc-1"

    async def append_document_blocks(
        self, document_id: str, blocks: list[dict]
    ) -> None:
        assert document_id == "delivery-doc-1"
        self.block_batches.append(blocks)

    async def upload_file_all(
        self, filename: str, content: bytes, mime_type: str
    ) -> str:
        assert mime_type
        self.direct_uploads.append((filename, content))
        return f"file-direct-{len(self.direct_uploads)}"

    async def prepare_file_upload(self, filename: str, size: int) -> tuple[str, int]:
        self.prepares.append((filename, size))
        return "upload-1", 8 * 1024 * 1024

    async def upload_file_part(
        self, upload_id: str, sequence: int, content: bytes
    ) -> None:
        self.parts.append((upload_id, sequence, content))

    async def finish_file_upload(self, upload_id: str, block_count: int) -> str:
        self.finishes.append((upload_id, block_count))
        return "file-chunked-1"

    async def add_document_member(
        self, document_id: str, owner_open_id: str
    ) -> None:
        self.members.append((document_id, owner_open_id))


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
                task_id="task-video",
                task_type="image_to_video",
                title="棋局短片",
                source_block_ids=["block-1"],
                user_intent="棋子移动",
                prompt="黑白棋子在棋盘上移动",
                reference_images=[
                    {"asset_id": "asset-1", "role": "reference_image", "order": 1}
                ],
                aspect_ratio="9:16",
                duration=5,
                resolution="720p",
                generate_audio=False,
                output_count=1,
            )
        ],
    )


def _artifact(path: Path, *, artifact_id: str = "artifact-1") -> Artifact:
    content = path.read_bytes()
    return Artifact(
        artifact_id=artifact_id,
        task_id="task-video",
        kind="video",
        local_path=path,
        mime_type="video/mp4",
        size=len(content),
        sha256=sha256(content).hexdigest(),
        provider_task_id="provider-task-1",
        status="ready",
    )


async def test_delivery_creates_expected_title_and_reuses_document(
    tmp_path: Path,
) -> None:
    output = tmp_path / "result.mp4"
    output.write_bytes(b"small-video")
    artifact = _artifact(output)
    repository = await Repository.open(tmp_path / "business.sqlite3")
    client = FakeDeliveryClient()
    writer = FeishuDeliveryWriter(
        client,
        repository,
        owner_open_id="ou_test",
        now=lambda: datetime(2026, 7, 20, 14, 30, tzinfo=UTC),
    )

    first = await writer.deliver("run-1", _document(), _plan(), [artifact])
    second = await writer.deliver("run-1", _document(), _plan(), [artifact])

    assert first.document_id == second.document_id == "delivery-doc-1"
    assert first.document_url.endswith("/delivery-doc-1")
    assert client.created_titles == ["[AI 交付] 棋局 - 2026-07-20 22:30"]
    assert len(client.direct_uploads) == 1
    assert len(client.block_batches) >= 1
    assert client.members == [("delivery-doc-1", "ou_test")]
    await repository.close()


async def test_video_larger_than_20mb_uses_resumable_chunk_upload(
    tmp_path: Path,
) -> None:
    output = tmp_path / "large.mp4"
    output.write_bytes(b"x" * (20 * 1024 * 1024 + 1))
    artifact = _artifact(output, artifact_id="artifact-large")
    repository = await Repository.open(tmp_path / "business.sqlite3")
    client = FakeDeliveryClient()
    writer = FeishuDeliveryWriter(
        client,
        repository,
        owner_open_id="ou_test",
    )

    record = await writer.deliver("run-large", _document(), _plan(), [artifact])

    assert record.uploaded_artifact_ids == ["artifact-large"]
    assert client.direct_uploads == []
    assert client.prepares == [("large.mp4", artifact.size)]
    assert len(client.parts) == 3
    assert [part[1] for part in client.parts] == [0, 1, 2]
    assert client.finishes == [("upload-1", 3)]
    saved = await repository.get_artifact("artifact-large")
    assert saved is not None and saved.feishu_file_token == "file-chunked-1"
    await repository.close()


async def test_real_client_delivery_methods_use_official_api_paths() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "tenant_access_token": "fiction-token",
                    "expire": 7200,
                },
            )
        if request.url.path == "/open-apis/docx/v1/documents":
            return httpx.Response(
                200, json={"code": 0, "data": {"document": {"document_id": "doc-1"}}}
            )
        if request.url.path.endswith("/children"):
            return httpx.Response(200, json={"code": 0, "data": {}})
        if request.url.path.endswith("upload_all"):
            assert request.headers["content-type"].startswith("multipart/form-data")
            return httpx.Response(200, json={"code": 0, "data": {"file_token": "file-1"}})
        if request.url.path.endswith("upload_prepare"):
            return httpx.Response(
                200,
                json={"code": 0, "data": {"upload_id": "upload-1", "block_size": 4}},
            )
        if request.url.path.endswith("upload_part"):
            return httpx.Response(200, json={"code": 0, "data": {}})
        if request.url.path.endswith("upload_finish"):
            return httpx.Response(200, json={"code": 0, "data": {"file_token": "file-2"}})
        if "/permissions/" in request.url.path:
            return httpx.Response(200, json={"code": 0, "data": {}})
        raise AssertionError(request.url)

    async with httpx.AsyncClient(
        base_url="https://open.feishu.cn",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = FeishuClient(
            Settings(
                lark_app_id="fiction-app",
                lark_app_secret="fiction-secret",
                lark_output_folder_token="fiction-folder",
            ),
            http_client=http_client,
        )
        assert await client.create_document("交付") == "doc-1"
        await client.append_document_blocks("doc-1", [{"block_type": 2}])
        assert await client.upload_file_all("a.mp4", b"123", "video/mp4") == "file-1"
        assert await client.prepare_file_upload("b.mp4", 8) == ("upload-1", 4)
        await client.upload_file_part("upload-1", 0, b"1234")
        assert await client.finish_file_upload("upload-1", 2) == "file-2"
        await client.add_document_member("doc-1", "ou_test")

    assert "/open-apis/docx/v1/documents/doc-1/blocks/doc-1/children" in paths
    assert "/open-apis/drive/v1/files/upload_all" in paths
    assert "/open-apis/drive/v1/files/upload_prepare" in paths
    assert "/open-apis/drive/v1/files/upload_part" in paths
    assert "/open-apis/drive/v1/files/upload_finish" in paths
    assert "/open-apis/drive/v1/permissions/doc-1/members" in paths


async def test_delivery_rejects_artifact_changed_after_generation(
    tmp_path: Path,
) -> None:
    output = tmp_path / "result.mp4"
    output.write_bytes(b"original")
    artifact = _artifact(output)
    output.write_bytes(b"tampered")
    repository = await Repository.open(tmp_path / "business.sqlite3")
    client = FakeDeliveryClient()
    writer = FeishuDeliveryWriter(
        client, repository, owner_open_id="ou_test"
    )

    with pytest.raises(ValueError, match="完整性"):
        await writer.deliver("run-tampered", _document(), _plan(), [artifact])

    assert client.direct_uploads == []
    assert client.prepares == []
    await repository.close()


async def test_retry_delivery_reuses_document_uploads_and_completed_blocks(
    tmp_path: Path,
) -> None:
    class PermissionFailsOnceClient(FakeDeliveryClient):
        def __init__(self) -> None:
            super().__init__()
            self.permission_attempts = 0

        async def add_document_member(
            self, document_id: str, owner_open_id: str
        ) -> None:
            self.permission_attempts += 1
            if self.permission_attempts == 1:
                raise RuntimeError("fictional permission failure")
            await super().add_document_member(document_id, owner_open_id)

    output = tmp_path / "result.mp4"
    output.write_bytes(b"small-video")
    artifact = _artifact(output)
    repository = await Repository.open(tmp_path / "business.sqlite3")
    client = PermissionFailsOnceClient()
    writer = FeishuDeliveryWriter(client, repository, owner_open_id="ou_test")

    with pytest.raises(RuntimeError, match="permission failure"):
        await writer.deliver("run-retry", _document(), _plan(), [artifact])
    block_batch_count = len(client.block_batches)
    retried = await writer.retry_delivery("run-retry")

    assert retried.status == "succeeded"
    assert len(client.created_titles) == 1
    assert len(client.direct_uploads) == 1
    assert len(client.block_batches) == block_batch_count
    assert client.members == [("delivery-doc-1", "ou_test")]
    await repository.close()


async def test_chunk_upload_resumes_after_completed_part(
    tmp_path: Path,
) -> None:
    class PartFailsOnceClient(FakeDeliveryClient):
        def __init__(self) -> None:
            super().__init__()
            self.failed = False

        async def upload_file_part(
            self, upload_id: str, sequence: int, content: bytes
        ) -> None:
            if sequence == 1 and not self.failed:
                self.failed = True
                raise RuntimeError("fictional part failure")
            await super().upload_file_part(upload_id, sequence, content)

    output = tmp_path / "large.mp4"
    output.write_bytes(b"x" * (20 * 1024 * 1024 + 1))
    artifact = _artifact(output, artifact_id="artifact-resume")
    repository = await Repository.open(tmp_path / "business.sqlite3")
    client = PartFailsOnceClient()
    writer = FeishuDeliveryWriter(client, repository, owner_open_id="ou_test")

    with pytest.raises(RuntimeError, match="part failure"):
        await writer.deliver("run-resume", _document(), _plan(), [artifact])
    await writer.deliver("run-resume", _document(), _plan(), [artifact])

    assert len(client.prepares) == 1
    assert [sequence for _, sequence, _ in client.parts].count(0) == 1
    assert [sequence for _, sequence, _ in client.parts] == [0, 1, 2]
    assert client.finishes == [("upload-1", 3)]
    await repository.close()


async def test_chunk_upload_rejects_file_changed_while_streaming(
    tmp_path: Path,
) -> None:
    output = tmp_path / "large.mp4"
    output.write_bytes(b"x" * (20 * 1024 * 1024 + 1))
    artifact = _artifact(output, artifact_id="artifact-race")

    class MutatingClient(FakeDeliveryClient):
        async def upload_file_part(
            self, upload_id: str, sequence: int, content: bytes
        ) -> None:
            await super().upload_file_part(upload_id, sequence, content)
            if sequence == 0:
                output.write_bytes(b"changed-during-upload")

    repository = await Repository.open(tmp_path / "business.sqlite3")
    client = MutatingClient()
    writer = FeishuDeliveryWriter(client, repository, owner_open_id="ou_test")

    with pytest.raises(ValueError, match="完整性"):
        await writer.deliver("run-race", _document(), _plan(), [artifact])

    assert client.finishes == []
    await repository.close()


async def test_retry_delivery_recovers_when_document_creation_failed(
    tmp_path: Path,
) -> None:
    class CreateFailsOnceClient(FakeDeliveryClient):
        def __init__(self) -> None:
            super().__init__()
            self.create_attempts = 0

        async def create_document(self, title: str) -> str:
            self.create_attempts += 1
            if self.create_attempts == 1:
                raise RuntimeError("fictional create failure")
            return await super().create_document(title)

    output = tmp_path / "result.mp4"
    output.write_bytes(b"small-video")
    artifact = _artifact(output)
    repository = await Repository.open(tmp_path / "business.sqlite3")
    await repository.save_artifact("run-create-retry", artifact)
    client = CreateFailsOnceClient()
    writer = FeishuDeliveryWriter(client, repository, owner_open_id="ou_test")

    with pytest.raises(RuntimeError, match="create failure"):
        await writer.deliver(
            "run-create-retry", _document(), _plan(), [artifact]
        )
    retried = await writer.retry_delivery("run-create-retry")

    assert retried.status == "succeeded"
    assert client.create_attempts == 2
    await repository.close()


async def test_delivery_document_lists_tasks_without_artifacts_as_failed(
    tmp_path: Path,
) -> None:
    repository = await Repository.open(tmp_path / "business.sqlite3")
    client = FakeDeliveryClient()
    writer = FeishuDeliveryWriter(client, repository, owner_open_id="ou_test")

    await writer.deliver("run-failed-task", _document(), _plan(), [])

    serialized = json.dumps(client.block_batches, ensure_ascii=False)
    assert "失败任务与重试建议" in serialized
    assert "棋局短片：未生成可交付产物" in serialized
    await repository.close()
