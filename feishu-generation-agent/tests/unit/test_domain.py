from inspect import signature
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ValidationError

from feishu_generation_agent.domain.artifact import Artifact, ProviderResult
from feishu_generation_agent.domain import document as document_domain
from feishu_generation_agent.domain.document import (
    MediaAsset,
    SourceType,
)
from feishu_generation_agent.domain.errors import AgentError, ErrorCategory, ErrorDetail
from feishu_generation_agent.domain.plan import (
    ExcludedAsset,
    GenerationTask,
    TaskPlan,
)
from feishu_generation_agent.ports import (
    DeliveryWriter,
    DocumentSource,
    ImageGenerator,
    RequirementPlanner,
    VideoGenerator,
    VisionAnalyzer,
)


def task_payload(task_type: str, task_id: str = "task-1") -> dict:
    return {
        "task_id": task_id,
        "task_type": task_type,
        "title": "熊猫拉抽屉",
        "source_block_ids": ["block-1"],
        "user_intent": "保持角色一致并完成动作",
        "prompt": "熊猫拉开抽屉，彩球滚出",
        "reference_images": [
            {"asset_id": "asset-1", "role": "reference_image", "order": 1}
        ],
        "aspect_ratio": "9:16",
        "output_count": 1,
    }


def image_task(task_id: str = "task-1", **updates: object) -> GenerationTask:
    payload = task_payload("image_to_image", task_id)
    payload.update(image_size="2K", **updates)
    return GenerationTask.model_validate(payload)


def video_task(task_id: str = "task-1", **updates: object) -> GenerationTask:
    payload = task_payload("image_to_video", task_id)
    payload.update(duration=10, resolution="720p", **updates)
    return GenerationTask.model_validate(payload)


def test_image_task_requires_image_size_and_rejects_video_fields():
    payload = task_payload("image_to_image")
    payload["image_size"] = "2K"
    assert GenerationTask.model_validate(payload).image_size == "2K"

    for field, value in (
        ("duration", 10),
        ("resolution", "720p"),
        ("generate_audio", False),
    ):
        invalid_payload = payload | {field: value}
        with pytest.raises(ValidationError, match=field):
            GenerationTask.model_validate(invalid_payload)

    # image_size 缺失时兜底为 2K：出图分辨率由我们统一决定（统一按最高档
    # 出图再裁到交付尺寸），不该因为 planner 漏填就让整个计划失败。
    del payload["image_size"]
    assert GenerationTask.model_validate(payload).image_size == "2K"


@pytest.mark.parametrize("generate_audio", [None, True, False])
def test_video_task_requires_duration_and_resolution(generate_audio: bool | None):
    payload = task_payload("image_to_video")
    payload.update(duration=10, resolution="720p", generate_audio=generate_audio)
    task = GenerationTask.model_validate(payload)
    assert task.duration == 10
    assert task.generate_audio is generate_audio

    for required_field in ("duration", "resolution"):
        invalid_payload = payload.copy()
        del invalid_payload[required_field]
        with pytest.raises(ValidationError, match=required_field):
            GenerationTask.model_validate(invalid_payload)


def test_video_task_rejects_image_size_and_all_tasks_require_references():
    payload = task_payload("image_to_video")
    payload.update(duration=10, resolution="720p", image_size="2K")
    with pytest.raises(ValidationError, match="image_size"):
        GenerationTask.model_validate(payload)

    for task_type, task_fields in (
        ("image_to_image", {"image_size": "2K"}),
        ("image_to_video", {"duration": 10, "resolution": "720p"}),
    ):
        payload = task_payload(task_type) | task_fields | {"reference_images": []}
        with pytest.raises(ValidationError, match="reference_images"):
            GenerationTask.model_validate(payload)


def test_reference_role_normalizes_saved_planner_alias():
    payload = task_payload("image_to_video")
    payload.update(duration=10, resolution="720p")
    payload["reference_images"][0]["role"] = "character_and_style_reference"

    task = GenerationTask.model_validate(payload)

    assert task.reference_images[0].role == "reference_image"


def test_video_task_normalizes_mixed_frames_to_multi_reference():
    task = video_task(
        reference_images=[
            {"asset_id": "first", "role": "first_frame", "order": 1},
            {"asset_id": "style", "role": "reference_image", "order": 2},
        ]
    )

    assert task.reference_mode == "multi_reference"
    assert [item.role for item in task.reference_images] == [
        "reference_image",
        "reference_image",
    ]
    assert "第 1 张参考图" in task.prompt


def test_video_task_keeps_exact_first_and_last_frames():
    task = video_task(
        reference_images=[
            {"asset_id": "first", "role": "first_frame", "order": 1},
            {"asset_id": "last", "role": "last_frame", "order": 2},
        ]
    )

    assert task.reference_mode == "first_last_frame"
    assert [item.role for item in task.reference_images] == [
        "first_frame",
        "last_frame",
    ]


def test_multi_reference_accepts_video_and_audio_roles():
    task = video_task(
        reference_images=[
            {"asset_id": "image-1", "role": "reference_image", "order": 1},
            {"asset_id": "video-1", "role": "reference_video", "order": 2},
            {"asset_id": "audio-1", "role": "reference_audio", "order": 3},
        ]
    )

    assert task.reference_mode == "multi_reference"
    assert [reference.role for reference in task.reference_images] == [
        "reference_image",
        "reference_video",
        "reference_audio",
    ]


def test_first_last_frame_rejects_media_reference_roles():
    with pytest.raises(ValidationError, match="first_last_frame"):
        video_task(
            reference_mode="first_last_frame",
            reference_images=[
                {"asset_id": "first", "role": "first_frame", "order": 1},
                {"asset_id": "audio", "role": "reference_audio", "order": 2},
            ],
        )


def test_reference_role_rejects_unknown_values():
    payload = task_payload("image_to_video")
    payload.update(duration=10, resolution="720p")
    payload["reference_images"][0]["role"] = "fictional_role"

    with pytest.raises(ValidationError, match="role"):
        GenerationTask.model_validate(payload)


@pytest.mark.parametrize(
    ("raw_resolution", "expected"),
    [
        ("1080x1920", "1080p"),
        ("1920x1080", "1080p"),
        ("720x1280", "720p"),
        ("1280x720", "720p"),
    ],
)
def test_video_resolution_normalizes_common_pixel_dimensions(
    raw_resolution: str,
    expected: str,
):
    payload = task_payload("image_to_video")
    payload.update(duration=15, resolution=raw_resolution)

    task = GenerationTask.model_validate(payload)

    assert task.resolution == expected


def test_video_resolution_rejects_unsupported_values():
    payload = task_payload("image_to_video")
    payload.update(duration=10, resolution="4k")

    with pytest.raises(ValidationError, match="resolution"):
        GenerationTask.model_validate(payload)


def test_blocking_task_cannot_be_approved():
    task = image_task(blocking_issues=["图片用途不明确"])
    plan = TaskPlan(tasks=[task])
    with pytest.raises(ValueError, match="blocking"):
        plan.approved_subset(["task-1"], max_output_count=4)


def test_plan_rejects_duplicate_task_ids_and_duplicate_selections():
    with pytest.raises(ValidationError, match="duplicate task_id"):
        TaskPlan(tasks=[image_task(), image_task()])

    plan = TaskPlan(tasks=[image_task()])
    with pytest.raises(ValueError, match="duplicate selected task_id"):
        plan.approved_subset(["task-1", "task-1"], max_output_count=4)


def test_approved_subset_rejects_unknown_ids_and_per_task_output_limit():
    plan = TaskPlan(tasks=[image_task(output_count=5)])
    with pytest.raises(ValueError, match="unknown"):
        plan.approved_subset(["missing"], max_output_count=4)
    with pytest.raises(ValueError, match="max_output_count"):
        plan.approved_subset(["task-1"], max_output_count=4)


def test_approved_subset_preserves_plan_order_and_document_summary():
    first = image_task("task-1")
    second = video_task("task-2")
    plan = TaskPlan(tasks=[first, second], document_summary="两项生成需求")

    approved = plan.approved_subset(
        ["task-2", "task-1"],
        max_output_count=4,
    )

    assert approved is not plan
    assert [task.task_id for task in approved.tasks] == ["task-1", "task-2"]
    assert approved.document_summary == "两项生成需求"


def test_task_plan_accepts_legacy_json_without_excluded_assets():
    plan = TaskPlan.model_validate(
        {"tasks": [image_task().model_dump(mode="json")]}
    )

    assert plan.excluded_assets == []
    assert "excluded_assets" in TaskPlan.model_json_schema()["required"]


def test_excluded_assets_require_chinese_unique_reasons_and_no_reference_overlap():
    with pytest.raises(ValidationError, match="必须包含中文"):
        ExcludedAsset(asset_id="asset-2", reason="not used")

    with pytest.raises(ValidationError, match="duplicate excluded asset_id"):
        TaskPlan(
            tasks=[image_task()],
            excluded_assets=[
                ExcludedAsset(asset_id="asset-2", reason="供应商数量限制"),
                ExcludedAsset(asset_id="asset-2", reason="用户没有选择"),
            ],
        )

    with pytest.raises(ValidationError, match="referenced and excluded"):
        TaskPlan(
            tasks=[image_task()],
            excluded_assets=[
                ExcludedAsset(asset_id="asset-1", reason="用户没有选择")
            ],
        )


def test_approved_subset_preserves_and_explains_assets_unused_by_selection():
    plan = TaskPlan(
        tasks=[
            image_task("task-1"),
            image_task(
                "task-2",
                reference_images=[
                    {
                        "asset_id": "asset-2",
                        "role": "reference_image",
                        "order": 1,
                    }
                ],
            ),
        ],
        excluded_assets=[
            ExcludedAsset(
                asset_id="asset-3",
                reason="供应商最多支持两张参考图，保留主体与场景图。",
            )
        ],
    )

    approved = plan.approved_subset(["task-1"], max_output_count=4)

    assert [item.asset_id for item in approved.excluded_assets] == [
        "asset-3",
        "asset-2",
    ]
    assert "未选择" in approved.excluded_assets[1].reason


def test_domain_models_dump_json_serializable_values():
    media = MediaAsset(
        asset_id="asset-1",
        source_block_id="block-1",
        origin="feishu",
        local_path=Path("/tmp/reference.png"),
        mime_type="image/png",
        size=123,
        sha256="abc",
    )
    artifact = Artifact(
        artifact_id="artifact-1",
        task_id="task-1",
        kind="image",
        local_path=Path("/tmp/result.png"),
        mime_type="image/png",
        size=456,
        sha256="def",
        status="ready",
    )

    assert media.model_dump(mode="json")["local_path"] == "/tmp/reference.png"
    assert artifact.model_dump(mode="json")["local_path"] == "/tmp/result.png"
    assert SourceType.DOCX.value == "docx"


def test_agent_error_exposes_serializable_detail():
    detail = ErrorDetail(
        category=ErrorCategory.VALIDATION,
        message="任务无效",
        technical_detail="missing image_size",
        retryable=False,
    )
    error = AgentError(detail)

    assert str(error) == "任务无效"
    assert error.detail.model_dump(mode="json")["category"] == "validation_error"


def test_ingest_issue_classification_supports_new_and_legacy_formats():
    whole_sheet_issues = [
        "阻塞：内嵌电子表格读取失败（Block sheet-block）：缺少 token",
        "阻塞：内嵌电子表格 NuBUx5 读取失败（Block sheet-block）：导出失败",
    ]
    asset_issues = [
        "素材失败：内嵌电子表格素材 sheet-1 保存失败",
        "素材失败：素材 image-1 下载失败",
        "阻塞：内嵌电子表格素材 sheet-legacy 保存失败",
        "阻塞：素材 image-legacy 下载失败",
    ]
    records = document_domain.resolve_ingest_issue_records(
        SimpleNamespace(
            ingest_issue_records=[],
            ingest_issues=[*whole_sheet_issues, *asset_issues],
        )
    )

    assert [record.severity for record in records] == [
        "blocking",
        "blocking",
        "asset",
        "asset",
        "asset",
        "asset",
    ]


def test_structured_ingest_record_is_the_classification_source_of_truth():
    assert hasattr(document_domain, "IngestIssueRecord")
    record_type = document_domain.IngestIssueRecord
    resolver = document_domain.resolve_ingest_issue_records
    record = record_type(
        severity="asset",
        code="media_download_failed",
        display_message="文档图片下载失败，其他素材可继续处理",
        source_block_id="image-block",
        asset_id="image-1",
    )
    document = SimpleNamespace(
        ingest_issue_records=[record],
        ingest_issues=["阻塞：内嵌电子表格 NuBUx5 读取失败X"],
    )

    resolved = resolver(document)

    assert resolved == [record]
    assert resolved[0].severity == "asset"


def test_structured_ingest_record_rejects_unallowlisted_display_message():
    assert hasattr(document_domain, "IngestIssueRecord")
    with pytest.raises(ValidationError, match="do not match"):
        document_domain.IngestIssueRecord(
            severity="blocking",
            code="sheet_export_failed",
            display_message="Bearer sk-secret-value",
            source_block_id="fiction-sheet",
        )


def test_structured_ingest_record_model_copy_revalidates_updates():
    record = document_domain.make_ingest_issue_record(
        document_domain.IngestIssueCode.MEDIA_DOWNLOAD_FAILED,
        asset_id="image-1",
    )

    with pytest.raises(ValidationError, match="do not match"):
        record.model_copy(update={"severity": "blocking"})


def test_ingest_issue_resolver_revalidates_existing_model_instances():
    record = document_domain.make_ingest_issue_record(
        document_domain.IngestIssueCode.MEDIA_DOWNLOAD_FAILED,
        asset_id="image-1",
    )
    forged = BaseModel.model_copy(
        record,
        update={
            "severity": document_domain.IngestIssueSeverity.BLOCKING,
        },
    )

    with pytest.raises(ValidationError, match="do not match"):
        document_domain.resolve_ingest_issue_records(
            SimpleNamespace(
                ingest_issue_records=[forged],
                ingest_issues=[],
            )
        )


@pytest.mark.parametrize(
    "unsafe_identifier",
    [
        "Bearer-secret-token",
        "sk-live-12345678",
        "image-sk-live-12345678",
        "ark-live-12345678",
        "block-ark-live-12345678",
        "AKLT1234567890",
        "image-AKLT1234567890",
        "C:\\Users\\alice\\private.txt",
    ],
)
def test_structured_ingest_record_rejects_secret_like_identifiers(
    unsafe_identifier: str,
):
    with pytest.raises(ValidationError, match="identifier|pattern"):
        document_domain.IngestIssueRecord(
            severity="asset",
            code="media_download_failed",
            display_message="文档图片下载失败，其他素材可继续处理",
            asset_id=unsafe_identifier,
        )


@pytest.mark.parametrize(
    "legacy_issue",
    [
        (
            "阻塞：内嵌电子表格 NuBUx5 读取失败（Block fiction-sheet）："
            "/Users/alice/private/secret-token.xlsx"
        ),
        "阻塞：素材 image-old 下载失败：Bearer sk-secret-value",
        "阻塞：内嵌电子表格 NuBUx5 读取失败X",
    ],
)
def test_malformed_or_sensitive_legacy_ingest_issue_fails_closed(
    legacy_issue: str,
):
    assert hasattr(document_domain, "resolve_ingest_issue_records")
    records = document_domain.resolve_ingest_issue_records(
        SimpleNamespace(ingest_issue_records=[], ingest_issues=[legacy_issue])
    )

    assert len(records) == 1
    assert records[0].severity == "blocking"
    assert records[0].code == "legacy_unknown"
    assert records[0].display_message == (
        "文档读取出现未知问题，请重新读取后再审批"
    )
    assert "secret" not in records[0].model_dump_json()
    assert "/Users/" not in records[0].model_dump_json()
    assert "Bearer" not in records[0].model_dump_json()


@pytest.mark.parametrize(
    ("legacy_issue", "severity", "code"),
    [
        (
            "阻塞：内嵌电子表格素材 sheet-old 保存失败",
            "asset",
            "sheet_asset_save_failed",
        ),
        (
            "阻塞：素材 image-old 下载失败",
            "asset",
            "media_download_failed",
        ),
        (
            (
                "素材失败：内嵌电子表格素材 sheet-current 保存失败"
                "（Block fiction-sheet，Sheet NuBUx5）：图片保存失败，请稍后重试"
            ),
            "asset",
            "sheet_asset_save_failed",
        ),
        (
            (
                "素材失败：素材 image-current 下载失败"
                "（Block fiction-image）：图片下载或保存失败，请稍后重试"
            ),
            "asset",
            "media_download_failed",
        ),
        (
            "阻塞：内嵌电子表格 NuBUx5 读取失败（Block fiction-sheet）",
            "blocking",
            "legacy_sheet_read_failed",
        ),
    ],
)
def test_strict_standard_legacy_formats_migrate_deterministically(
    legacy_issue: str,
    severity: str,
    code: str,
):
    assert hasattr(document_domain, "resolve_ingest_issue_records")
    records = document_domain.resolve_ingest_issue_records(
        SimpleNamespace(ingest_issue_records=[], ingest_issues=[legacy_issue])
    )

    assert [(record.severity, record.code) for record in records] == [
        (severity, code)
    ]


def test_known_legacy_timeout_preserves_safe_actionable_reason():
    assert hasattr(document_domain, "resolve_ingest_issue_records")
    records = document_domain.resolve_ingest_issue_records(
        SimpleNamespace(
            ingest_issue_records=[],
            ingest_issues=[
                (
                    "阻塞：内嵌电子表格 NuBUx5 读取失败"
                    "（Block fiction-sheet）："
                    "飞书电子表格导出超时，请稍后重试"
                )
            ],
        )
    )

    assert records[0].severity == "blocking"
    assert records[0].code == "sheet_export_timeout"
    assert records[0].display_message == "飞书电子表格导出超时，请稍后重试"


def test_provider_result_url_requires_explicit_untrusted_boundary() -> None:
    with pytest.raises(ValidationError, match="url_trust"):
        ProviderResult(url="https://cdn.example/result.png", mime_type="image/png")

    result = ProviderResult(
        url="https://cdn.example/result.png",
        url_trust="untrusted",
        mime_type="image/png",
    )
    assert result.url_trust == "untrusted"


def test_provider_result_local_file_requires_integrity_metadata(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValidationError, match="size"):
        ProviderResult(local_path=tmp_path / "result.png", mime_type="image/png")

    result = ProviderResult(
        local_path=tmp_path / "result.png",
        mime_type="image/png",
        size=12,
        sha256="a" * 64,
    )
    assert result.local_path == tmp_path / "result.png"


def test_all_six_adapter_protocols_are_public():
    assert {
        DocumentSource.__name__,
        VisionAnalyzer.__name__,
        RequirementPlanner.__name__,
        ImageGenerator.__name__,
        VideoGenerator.__name__,
        DeliveryWriter.__name__,
    } == {
        "DocumentSource",
        "VisionAnalyzer",
        "RequirementPlanner",
        "ImageGenerator",
        "VideoGenerator",
        "DeliveryWriter",
    }


def test_paid_generator_protocols_accept_preassociated_submission_id() -> None:
    for protocol in (ImageGenerator, VideoGenerator):
        parameter = signature(protocol.submit).parameters["submission_id"]
        assert parameter.kind.name == "KEYWORD_ONLY"
        assert parameter.default is None
