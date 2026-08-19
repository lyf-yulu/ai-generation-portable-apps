"""image_size 只能是 1K/1.5K/2K；像素尺寸属于 size_variants。

真实故障：planner 把需求文档里的「尺寸：1700*2500」填进了 image_size，
size_variants 留空。后果有两个——
1. seedream_size("1700x2500") 直接抛 ValueError，出图必失败
2. size_variants 为空导致刚接通的 resize 不触发，出图不会被裁到要求尺寸

需求文档原文就是写「尺寸：1700*2500」，模型混淆是自然的，所以在领域层
自动归一化，而不是只靠契约措辞约束。
"""

import pytest
from pydantic import ValidationError

from feishu_generation_agent.domain.plan import GenerationTask


def _payload(**updates: object) -> dict:
    payload = {
        "task_id": "cg-1",
        "task_type": "image_to_image",
        "title": "Victor 中景",
        "source_block_ids": ["b1"],
        "user_intent": "出 CG 图",
        "prompt": "@图片1 中的男性中景，戏剧化顶光",
        "reference_images": [
            {"asset_id": "a1", "role": "reference_image", "order": 1}
        ],
        "aspect_ratio": "9:16",
        "image_size": "2K",
    }
    payload.update(updates)
    return payload


def test_pixel_image_size_moves_into_size_variants():
    task = GenerationTask.model_validate(
        _payload(image_size="1700x2500", size_variants=[])
    )

    assert task.image_size == "2K"
    assert task.size_variants == ["1700x2500"]


def test_pixel_image_size_accepts_star_separator():
    task = GenerationTask.model_validate(
        _payload(image_size="1700*2500", size_variants=[])
    )

    assert task.image_size == "2K"
    assert task.size_variants == ["1700x2500"]


def test_pixel_size_always_uses_2k_baseline():
    """统一按 2K 出图再裁，保证分辨率总是够用。"""
    task = GenerationTask.model_validate(
        _payload(image_size="640x640", size_variants=[])
    )

    assert task.image_size == "2K"
    assert task.size_variants == ["640x640"]


def test_large_pixel_size_also_uses_2k():
    task = GenerationTask.model_validate(
        _payload(image_size="1700x2500", size_variants=[])
    )

    assert task.image_size == "2K"


def test_missing_image_size_defaults_to_2k():
    payload = _payload()
    del payload["image_size"]

    task = GenerationTask.model_validate(payload)

    assert task.image_size == "2K"


def test_existing_variant_is_not_duplicated():
    task = GenerationTask.model_validate(
        _payload(image_size="1700x2500", size_variants=["1700x2500"])
    )

    assert task.size_variants == ["1700x2500"]


def test_valid_token_is_left_alone():
    task = GenerationTask.model_validate(
        _payload(image_size="2K", size_variants=["1700x2500"])
    )

    assert task.image_size == "2K"
    assert task.size_variants == ["1700x2500"]


@pytest.mark.parametrize("token", ["1K", "1.5K", "2K"])
def test_all_supported_tokens_pass(token: str):
    task = GenerationTask.model_validate(_payload(image_size=token))

    assert task.image_size == token


def test_unsupported_token_is_rejected():
    with pytest.raises(ValidationError):
        GenerationTask.model_validate(_payload(image_size="8K"))


def test_garbage_image_size_is_rejected():
    with pytest.raises(ValidationError):
        GenerationTask.model_validate(_payload(image_size="很大"))


def test_video_task_still_forbids_image_size():
    with pytest.raises(ValidationError):
        GenerationTask.model_validate(
            {
                "task_id": "v1",
                "task_type": "image_to_video",
                "title": "熊猫",
                "source_block_ids": ["b1"],
                "user_intent": "动作",
                "prompt": "熊猫拉抽屉",
                "reference_images": [
                    {"asset_id": "a1", "role": "reference_image", "order": 1}
                ],
                "aspect_ratio": "9:16",
                "duration": 5,
                "resolution": "720p",
                "image_size": "2K",
            }
        )
