"""槽位缺失时从 prompt 文本反解，而不是让任务失败。

真实故障与两次修正：
1. 方案 2 上线后 prompt 仍是视频三段式。查出 prompt_slots 为 null——它在
   schema 里但不在 required，模型只填必填的 prompt，把槽位内容以
   「景别：中景；时间与场景：…；动作：…」的形式写进了 prompt 文本。
2. 第一次改成「缺槽位就抛校验错」，结果整个测试套件从 20 秒变成跑不完
   （多处 fake planner 产出的图片任务本来就没有槽位）。硬失败是错的方向。

最终做法：模型已经在写带标签的结构化文本，直接解析它。解析不出来就保留
模型原文，不阻断任务——出图比不出图重要。
"""

from feishu_generation_agent.domain.image_prompt import parse_prompt_slots
from feishu_generation_agent.domain.plan import GenerationTask


_LABELLED_PROMPT = (
    "总体设定与素材绑定：本镜头生成 Victor 中景 CG。"
    "景别：中景；时间与场景：室内豪华厅堂；"
    "主体融合：Victor 如 @图片1 所示，脸部因愤怒扭曲；"
    "动作：双手握拳，怒视前方；背景：粉色帷幔与木地板；"
    "风格：3D 卡通渲染，参考 @图片4；画布：1700*2500；"
    "氛围：紧张对峙；时间：白天。"
)


def _payload(**updates: object) -> dict:
    payload = {
        "task_id": "cg-1",
        "task_type": "image_to_image",
        "title": "Victor 中景",
        "source_block_ids": ["b1"],
        "user_intent": "出 CG 图",
        "prompt": _LABELLED_PROMPT,
        "reference_images": [
            {"asset_id": "a1", "role": "reference_image", "order": 1}
        ],
        "aspect_ratio": "9:16",
        "image_size": "2K",
    }
    payload.update(updates)
    return payload


def test_parses_labelled_slots_from_prompt():
    slots = parse_prompt_slots(_LABELLED_PROMPT)

    assert slots is not None
    assert slots.shot == "中景"
    assert slots.action == "双手握拳，怒视前方"
    assert slots.canvas == "1700*2500"
    assert slots.mood == "紧张对峙"


def test_parsed_slots_keep_reference_tokens():
    slots = parse_prompt_slots(_LABELLED_PROMPT)

    assert slots is not None
    assert "@图片1" in slots.subject_integration
    assert "@图片4" in slots.style


def test_unlabelled_prompt_yields_none():
    assert parse_prompt_slots("一张竖版海报，光线明亮") is None


def test_task_assembles_template_from_parsed_slots():
    """模型没填 prompt_slots，但 prompt 里带标签时仍要拼出模板。"""
    task = GenerationTask.model_validate(_payload())

    assert task.prompt.startswith("禁止勾勒边缘线，")
    assert task.prompt.count("禁止勾勒边缘线") == 3
    assert "画面主次分明" in task.prompt
    assert "@图片1" in task.prompt


def test_explicit_slots_win_over_parsing():
    task = GenerationTask.model_validate(
        _payload(
            prompt_slots={
                "shot": "特写",
                "action": "抬手遮光",
                "canvas": "1700*2500",
            }
        )
    )

    assert "特写" in task.prompt
    assert "抬手遮光" in task.prompt


def test_unparsable_prompt_is_left_untouched():
    """解析不出槽位时保留原文，不阻断出图。"""
    task = GenerationTask.model_validate(_payload(prompt="一张竖版海报"))

    assert task.prompt == "一张竖版海报"


def test_video_task_is_never_reassembled():
    task = GenerationTask.model_validate(
        {
            "task_id": "v1",
            "task_type": "image_to_video",
            "title": "熊猫",
            "source_block_ids": ["b1"],
            "user_intent": "动作",
            "prompt": "景别：中景；动作：熊猫拉抽屉",
            "reference_images": [
                {"asset_id": "a1", "role": "reference_image", "order": 1}
            ],
            "aspect_ratio": "9:16",
            "duration": 5,
            "resolution": "720p",
        }
    )

    assert task.prompt == "景别：中景；动作：熊猫拉抽屉"
    assert "禁止勾勒边缘线" not in task.prompt


def test_tokens_outside_labelled_segments_are_preserved():
    """真实故障：模型把风格参考 token 写在「总体设定」段里，解析只取标签段，
    这些 token 全丢，拼装后校验判「缺少素材引用 @图片4/5/6」，run 失败。
    """
    prompt = (
        "总体设定与素材绑定：参考@图片4、@图片5、@图片6 中的完成图。"
        "景别：中景；主体融合：Victor 如@图片1 所示；动作：握拳；"
        "背景：帷幔；风格：3D 卡通渲染；画布：1700*2500；氛围：紧张"
    )

    task = GenerationTask.model_validate(_payload(prompt=prompt))

    for token in ("@图片1", "@图片4", "@图片5", "@图片6"):
        assert token in task.prompt, f"token 丢失：{token}"


def test_preserved_tokens_are_not_duplicated():
    prompt = (
        "景别：中景；主体融合：@图片1 中的角色；动作：握拳；"
        "背景：帷幔；风格：参考@图片4 的画风；画布：1700*2500"
    )

    task = GenerationTask.model_validate(_payload(prompt=prompt))

    assert task.prompt.count("@图片4") == 1
    assert task.prompt.count("@图片1") == 1
