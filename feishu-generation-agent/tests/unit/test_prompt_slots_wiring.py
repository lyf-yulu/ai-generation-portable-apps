"""planner 填槽位后，prompt 必须由代码覆盖重写。

模型仍会自己写一版 prompt（schema 要求 prompt 非空），但只要它给了槽位，
最终 prompt 就以代码拼装的为准——这是方案 2 的关键：模型没有跑偏空间。
"""

from feishu_generation_agent.domain.plan import GenerationTask


def _payload(**updates: object) -> dict:
    payload = {
        "task_id": "cg-1",
        "task_type": "image_to_image",
        "title": "Victor 中景",
        "source_block_ids": ["b1"],
        "user_intent": "出 CG 图",
        "prompt": "模型自己写的一段随意描述",
        "reference_images": [
            {"asset_id": "a1", "role": "reference_image", "order": 1}
        ],
        "aspect_ratio": "9:16",
        "image_size": "2K",
    }
    payload.update(updates)
    return payload


_SLOTS = {
    "shot": "中景",
    "time_and_scene": "白天，儿童房内",
    "subject_integration": "@图片1 中的男性角色自然站在房间中央",
    "action": "面部因愤怒而扭曲，双手握拳",
    "background": "儿童房的玩具与家具",
    "style": "3D 卡通迪士尼风格",
    "canvas": "1700*2500",
    "mood": "紧张愤怒",
    "time_of_day": "白天",
}


def test_slots_override_model_written_prompt():
    task = GenerationTask.model_validate(_payload(prompt_slots=_SLOTS))

    assert task.prompt.startswith("禁止勾勒边缘线，")
    assert "模型自己写的" not in task.prompt
    assert task.prompt.count("禁止勾勒边缘线") == 3


def test_prompt_survives_without_slots():
    """槽位缺失时保留模型写的 prompt，不能把任务弄空。"""
    task = GenerationTask.model_validate(_payload())

    assert task.prompt == "模型自己写的一段随意描述"


def test_assembled_prompt_keeps_reference_tokens():
    """@图片N 必须留在 prompt 里，否则参考图校验会判缺少引用。"""
    task = GenerationTask.model_validate(_payload(prompt_slots=_SLOTS))

    assert "@图片1" in task.prompt


def test_video_task_ignores_slots():
    """视频任务不套图片模板。"""
    task = GenerationTask.model_validate(
        {
            "task_id": "v1",
            "task_type": "image_to_video",
            "title": "熊猫",
            "source_block_ids": ["b1"],
            "user_intent": "动作",
            "prompt": "熊猫拉抽屉，镜头缓慢推进",
            "reference_images": [
                {"asset_id": "a1", "role": "reference_image", "order": 1}
            ],
            "aspect_ratio": "9:16",
            "duration": 5,
            "resolution": "720p",
        }
    )

    assert task.prompt == "熊猫拉抽屉，镜头缓慢推进"
    assert "禁止勾勒边缘线" not in task.prompt


def test_canvas_defaults_to_delivery_variant():
    """槽位没给画布时用交付尺寸兜底，避免模板缺这一句。"""
    slots = dict(_SLOTS)
    slots["canvas"] = ""
    task = GenerationTask.model_validate(
        _payload(prompt_slots=slots, size_variants=["1700x2500"])
    )

    assert "1700*2500" in task.prompt or "1700x2500" in task.prompt
