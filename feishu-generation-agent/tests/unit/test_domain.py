from inspect import signature
from pathlib import Path

import pytest
from pydantic import ValidationError

from feishu_generation_agent.domain.artifact import Artifact, ProviderResult
from feishu_generation_agent.domain.document import MediaAsset, SourceType
from feishu_generation_agent.domain.errors import AgentError, ErrorCategory, ErrorDetail
from feishu_generation_agent.domain.plan import GenerationTask, TaskPlan
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

    del payload["image_size"]
    with pytest.raises(ValidationError, match="image_size"):
        GenerationTask.model_validate(payload)


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
