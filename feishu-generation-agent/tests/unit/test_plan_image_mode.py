import pytest
from pydantic import ValidationError

from feishu_generation_agent.domain.plan import GenerationTask, TaskType


def _image_payload(**updates: object) -> dict:
    payload = {
        "task_id": "task-1",
        "task_type": "image_to_image",
        "title": "Victor 中景",
        "source_block_ids": ["block-1"],
        "user_intent": "出一张愤怒表情的 CG 图",
        "prompt": "@图片1 中的男性中景，脸部因愤怒扭曲，戏剧化顶光 + 侧逆光",
        "reference_images": [
            {"asset_id": "asset-1", "role": "reference_image", "order": 1}
        ],
        "aspect_ratio": "9:16",
        "image_size": "2K",
        "output_count": 1,
    }
    payload.update(updates)
    return payload


def _video_payload(**updates: object) -> dict:
    payload = {
        "task_id": "task-1",
        "task_type": "image_to_video",
        "title": "熊猫拉抽屉",
        "source_block_ids": ["block-1"],
        "user_intent": "保持角色一致并完成动作",
        "prompt": "熊猫拉开抽屉，彩球滚出",
        "reference_images": [
            {"asset_id": "asset-1", "role": "reference_image", "order": 1}
        ],
        "aspect_ratio": "9:16",
        "duration": 10,
        "resolution": "720p",
        "output_count": 1,
    }
    payload.update(updates)
    return payload


def test_image_task_defaults_provider_to_banana():
    task = GenerationTask.model_validate(_image_payload())
    assert task.task_type is TaskType.IMAGE_TO_IMAGE
    # 字段本身保持 None，避免 model_dump() 携带隐式 provider
    assert task.image_provider is None
    assert task.resolved_image_provider == "banana"
    assert task.size_variants == []
    assert task.safe_area is None


def test_image_dump_does_not_leak_implicit_provider():
    """图片 task dump 后改 task_type 造视频 task 不应撞上 video 护栏。"""
    image = GenerationTask.model_validate(_image_payload())
    video = GenerationTask.model_validate(
        image.model_dump()
        | {
            "task_type": "image_to_video",
            "image_size": None,
            "duration": 5,
            "resolution": "720p",
        }
    )
    assert video.task_type is TaskType.IMAGE_TO_VIDEO
    assert video.resolved_image_provider is None


def test_image_task_accepts_all_three_providers():
    for provider in ("seedream", "banana", "gpt-image2"):
        task = GenerationTask.model_validate(
            _image_payload(image_provider=provider)
        )
        assert task.image_provider == provider


def test_image_task_rejects_unknown_provider():
    with pytest.raises(ValidationError):
        GenerationTask.model_validate(_image_payload(image_provider="midjourney"))


def test_image_task_keeps_size_variants_and_safe_area():
    task = GenerationTask.model_validate(
        _image_payload(
            size_variants=["1080x2080", "1700x2500"],
            safe_area="1080x2080",
        )
    )
    assert task.size_variants == ["1080x2080", "1700x2500"]
    assert task.safe_area == "1080x2080"


def test_image_task_normalizes_size_variant_separator():
    task = GenerationTask.model_validate(
        _image_payload(size_variants=["1080*2080", "1700×2500"])
    )
    assert task.size_variants == ["1080x2080", "1700x2500"]


def test_image_task_rejects_malformed_size_variant():
    with pytest.raises(ValidationError):
        GenerationTask.model_validate(_image_payload(size_variants=["big"]))


def test_image_task_allows_multiple_outputs():
    task = GenerationTask.model_validate(_image_payload(output_count=3))
    assert task.output_count == 3


def test_video_task_rejects_image_only_fields():
    for field, value in (
        ("image_provider", "banana"),
        ("size_variants", ["1080x2080"]),
        ("safe_area", "1080x2080"),
    ):
        with pytest.raises(ValidationError):
            GenerationTask.model_validate(_video_payload(**{field: value}))


def test_video_task_still_validates_without_image_fields():
    task = GenerationTask.model_validate(_video_payload())
    assert task.task_type is TaskType.IMAGE_TO_VIDEO
    assert task.image_provider is None
    assert task.size_variants == []
    assert task.safe_area is None
