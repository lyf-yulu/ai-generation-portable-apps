"""画布尺寸必须是交付尺寸，不能是安全区。

真实故障：模型在 prompt 里写「画布：1080*2080」——那是安全区，不是交付
尺寸。反解原样采信，拼出的 prompt 变成「画面主体控制在画布1080*2080画面
中央」，比实际交付的 1700x2500 小了一圈，构图会偏。

安全区是构图界限（关键内容不出界），画布是出图尺寸，两者不能混。
"""

from feishu_generation_agent.domain.plan import GenerationTask


def _payload(**updates: object) -> dict:
    payload = {
        "task_id": "cg-1",
        "task_type": "image_to_image",
        "title": "Victor 中景",
        "source_block_ids": ["b1"],
        "user_intent": "出 CG 图",
        "prompt": (
            "景别：中景；主体融合：@图片1 中的角色；动作：握拳；"
            "背景：帷幔；风格：3D 卡通；画布：1080*2080；氛围：紧张"
        ),
        "reference_images": [
            {"asset_id": "a1", "role": "reference_image", "order": 1}
        ],
        "aspect_ratio": "9:16",
        "image_size": "2K",
        "safe_area": "1080x2080",
        "size_variants": ["1700x2500"],
    }
    payload.update(updates)
    return payload


def test_canvas_uses_delivery_size_not_safe_area():
    task = GenerationTask.model_validate(_payload())

    assert "画布1700*2500画面中央" in task.prompt
    assert "画布1080*2080" not in task.prompt


def test_canvas_kept_when_model_writes_delivery_size():
    """模型写对时不要改动。"""
    task = GenerationTask.model_validate(
        _payload(
            prompt=(
                "景别：中景；主体融合：@图片1 中的角色；动作：握拳；"
                "背景：帷幔；风格：3D 卡通；画布：1700*2500；氛围：紧张"
            )
        )
    )

    assert "画布1700*2500画面中央" in task.prompt


def test_canvas_falls_back_to_delivery_size_when_absent():
    task = GenerationTask.model_validate(
        _payload(
            prompt=(
                "景别：中景；主体融合：@图片1 中的角色；动作：握拳；"
                "背景：帷幔；风格：3D 卡通；氛围：紧张"
            )
        )
    )

    assert "画布1700*2500画面中央" in task.prompt


def test_no_safe_area_leaves_model_canvas_alone():
    """没有安全区时不做替换，避免误改模型给的合法值。"""
    task = GenerationTask.model_validate(
        _payload(safe_area=None, size_variants=["1700x2500"])
    )

    assert "画布1080*2080画面中央" in task.prompt
