import base64
from pathlib import Path
from typing import Any

import pytest

from feishu_generation_agent.config import Settings
from feishu_generation_agent.domain.artifact import (
    DeliveryRecord,
    ProviderResult,
    ProviderSubmission,
)
from feishu_generation_agent.domain.document import (
    DocumentBlock,
    MediaAsset,
    NormalizedDocument,
    SourceType,
    VisionDescription,
)
from feishu_generation_agent.domain.plan import (
    AuditReport,
    GenerationTask,
    TaskPlan,
)
from feishu_generation_agent.graph.nodes import GraphServices
from feishu_generation_agent.storage.files import FileStore
from feishu_generation_agent.storage.repository import Repository


class FakeGraphDocumentSource:
    def __init__(self, document: NormalizedDocument) -> None:
        self.document = document
        self.ingest_calls = 0
        self.revision_calls = 0

    async def ingest(self, request: Any) -> NormalizedDocument:
        self.ingest_calls += 1
        assert request.source_url.startswith("https://")
        return self.document

    async def get_revision(self, source_url: str) -> int:
        self.revision_calls += 1
        assert source_url.startswith("https://")
        return self.document.revision


class FakeGraphVisionAnalyzer:
    def __init__(self) -> None:
        self.calls = 0

    async def analyze(self, asset: MediaAsset) -> VisionDescription:
        self.calls += 1
        return VisionDescription(
            asset_id=asset.asset_id,
            subjects=["蓝色纸船"],
            scene="虚构的小河",
            style="柔和插画",
            composition="纸船位于画面中央",
            characters=[],
            actions=["向远处漂流"],
            visible_text=[],
            colors=["蓝色", "绿色"],
            probable_role="主体与场景参考图",
            uncertainties=[],
        )


class FakeGraphPlanner:
    def __init__(self, task: GenerationTask) -> None:
        self.task = task
        self.plan_calls = 0
        self.audit_calls = 0
        self.feedback: list[str | None] = []

    async def plan(
        self,
        document: NormalizedDocument,
        descriptions: list[VisionDescription],
        feedback: str | None,
    ) -> TaskPlan:
        self.plan_calls += 1
        self.feedback.append(feedback)
        assert document.document_id == "doc-graph"
        assert [description.asset_id for description in descriptions] == [
            "asset-1"
        ]
        prompt = self.task.prompt
        if feedback:
            prompt = f"{prompt}；用户反馈：{feedback}"
        return TaskPlan(
            tasks=[self.task.model_copy(update={"prompt": prompt})],
            document_summary="纸船连续漂流视频",
        )

    async def audit(
        self,
        document: NormalizedDocument,
        plan: TaskPlan,
    ) -> AuditReport:
        self.audit_calls += 1
        assert document.document_id == "doc-graph"
        assert len(plan.tasks) == 1
        return AuditReport()


class FakePaidGenerator:
    def __init__(self, provider: str) -> None:
        self.provider = provider
        self.submit_calls = 0
        self.poll_calls = 0

    async def submit(
        self,
        task: GenerationTask,
        assets: list[MediaAsset],
        *,
        submission_id: str | None = None,
    ) -> ProviderSubmission:
        self.submit_calls += 1
        assert isinstance(submission_id, str) and len(submission_id) == 32
        is_image = self.provider == "chiyun"
        content = (
            base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
            )
            if is_image
            else b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2"
        )
        return ProviderSubmission(
            provider=self.provider,
            provider_task_id=(
                submission_id if is_image else "fictional-seedance-official"
            ),
            status="succeeded",
            result_items=[
                ProviderResult(
                    base64_data=base64.b64encode(content).decode("ascii"),
                    mime_type="image/png" if is_image else "video/mp4",
                )
            ],
        )

    async def poll(self, submission: ProviderSubmission) -> ProviderSubmission:
        self.poll_calls += 1
        return submission


class FakeGraphDeliveryWriter:
    def __init__(self) -> None:
        self.deliver_calls = 0

    async def deliver(
        self,
        run_id: str,
        document: NormalizedDocument,
        plan: TaskPlan,
        artifacts: list[Any],
    ) -> DeliveryRecord:
        assert run_id
        self.deliver_calls += 1
        return DeliveryRecord(
            document_id=document.document_id,
            document_url="https://fiction.feishu.cn/docx/output",
            status="succeeded",
        )


@pytest.fixture
async def fake_services(tmp_path: Path):
    image_path = tmp_path / "data" / "reference.png"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(b"fictional-graph-image-bytes")
    asset = MediaAsset(
        asset_id="asset-1",
        source_block_id="image-1",
        origin="feishu",
        file_token="fictional-file-token",
        local_path=image_path,
        mime_type="image/png",
        size=image_path.stat().st_size,
        sha256="graph-sha-asset-1",
        width=640,
        height=480,
    )
    document = NormalizedDocument(
        document_id="doc-graph",
        title="纸船审批测试",
        revision=7,
        source_type=SourceType.DOCX,
        source_token="doc-graph",
        blocks=[
            DocumentBlock(
                block_id="page-1",
                parent_id=None,
                block_type="page",
                order=0,
                path=["page-1"],
                text="纸船审批测试",
            ),
            DocumentBlock(
                block_id="story-1",
                parent_id="page-1",
                block_type="text",
                order=1,
                path=["page-1", "story-1"],
                text="让纸船连续漂向远处。",
            ),
            DocumentBlock(
                block_id="image-1",
                parent_id="page-1",
                block_type="image",
                order=2,
                path=["page-1", "image-1"],
                image_asset_id="asset-1",
            ),
        ],
        text_view=(
            "[block:story-1] 让纸船连续漂向远处。\n"
            "[block:image-1] [image:asset-1]"
        ),
        media_assets=[asset],
    )
    task = GenerationTask(
        task_id="task-video",
        task_type="image_to_video",
        title="纸船漂流短片",
        source_block_ids=["story-1"],
        user_intent="生成连续漂流视频",
        prompt="蓝色纸船从近景连续漂向远处",
        reference_images=[
            {"asset_id": "asset-1", "role": "reference_image", "order": 1}
        ],
        aspect_ratio="16:9",
        duration=10,
        resolution="720p",
        generate_audio=False,
        output_count=1,
        confidence=0.95,
    )
    settings = Settings(
        data_dir=tmp_path / "data",
        outputs_dir=tmp_path / "outputs",
        business_db_path=tmp_path / "business.sqlite3",
        checkpoint_db_path=tmp_path / "checkpoints.sqlite3",
        lark_app_id="fictional-app-id",
        lark_app_secret="fictional-lark-key-must-not-persist",
        deepseek_api_key="fictional-deepseek-key-must-not-persist",
        claude_api_key="fictional-claude-key-must-not-persist",
        chiyun_api_key="fictional-chiyun-key-must-not-persist",
        ark_api_key="fictional-ark-key-must-not-persist",
        max_output_count=4,
        provider_poll_interval_seconds=0,
        provider_poll_max_attempts=4,
    )
    repository = await Repository.open(settings.business_db_path)
    services = GraphServices(
        document_source=FakeGraphDocumentSource(document),
        vision_analyzer=FakeGraphVisionAnalyzer(),
        planner=FakeGraphPlanner(task),
        image_generator=FakePaidGenerator("chiyun"),
        video_generator=FakePaidGenerator("seedance"),
        delivery_writer=FakeGraphDeliveryWriter(),
        repository=repository,
        file_store=FileStore(
            settings.data_dir,
            settings.outputs_dir,
            max_bytes=settings.max_download_bytes,
        ),
        settings=settings,
    )
    try:
        yield services
    finally:
        await repository.close()
