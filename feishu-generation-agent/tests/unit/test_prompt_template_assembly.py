"""模板由代码拼装，不靠模型记得写。

真实故障：图片模板改了四轮，产出的 prompt 时而套用模板、时而退回视频三段式
（「总体设定与素材绑定 → 镜头 → 风格与约束」）。契约本身已验证正确且确实送到
模型面前（快照 2294 字、含全部模板关键句、无 Seedance 残留），所以问题不是
代码 bug，而是模型对指令的遵守不稳定。

需求方那套约束句是踩坑总结出来的、必须一字不差。改由代码拼装：模型只填槽位，
固定句式由 build_image_prompt 拼出来，模型没有跑偏空间。
"""

from feishu_generation_agent.domain.image_prompt import (
    ImagePromptSlots,
    build_image_prompt,
)


def _slots(**updates: object) -> ImagePromptSlots:
    payload: dict[str, object] = {
        "shot": "中景",
        "time_and_scene": "白天，儿童房内",
        "subject_integration": "@图片1 中的男性角色自然站在房间中央",
        "action": "面部因愤怒而扭曲，双手握拳",
        "background": "儿童房的玩具与家具",
        "style": "3D 卡通迪士尼风格，色彩浓郁",
        "canvas": "1700*2500",
        "mood": "紧张愤怒",
        "time_of_day": "白天",
    }
    payload.update(updates)
    return ImagePromptSlots.model_validate(payload)


def test_prompt_starts_and_ends_with_edge_line_ban():
    prompt = build_image_prompt(_slots())

    assert prompt.startswith("禁止勾勒边缘线，")
    assert prompt.rstrip().endswith("禁止出现窗户")


def test_all_fixed_clauses_are_present():
    prompt = build_image_prompt(_slots())

    for clause in (
        "画面风格严格参考图一重新生成图片",
        "画面主次分明",
        "光影自然",
        "冷暖对比",
        "光线暖柔",
        "整体画面高亮度高明度",
        "画面风格严格参考图一生成图片",
        "禁止拉伸图片",
        "禁止出现窗户",
    ):
        assert clause in prompt, f"固定句式缺失：{clause}"


def test_edge_line_ban_appears_three_times():
    """模板里刻意重复三次以强化约束。"""
    prompt = build_image_prompt(_slots())

    assert prompt.count("禁止勾勒边缘线") == 3


def test_slots_are_substituted_in_order():
    prompt = build_image_prompt(_slots())

    positions = [
        prompt.index("中景"),
        prompt.index("儿童房的玩具与家具"),
        prompt.index("1700*2500"),
        prompt.index("紧张愤怒"),
    ]
    assert positions == sorted(positions), "槽位顺序必须与模板一致"


def test_canvas_is_wrapped_by_template_wording():
    prompt = build_image_prompt(_slots())

    assert "画面主体控制在画布1700*2500画面中央" in prompt


def test_night_scene_does_not_hardcode_daytime():
    prompt = build_image_prompt(
        _slots(time_of_day="夜晚", time_and_scene="夜晚，卧室内")
    )

    assert prompt.rstrip().endswith("夜晚，禁止出现窗户")


def test_window_ban_is_kept_even_for_window_scenes():
    """反 AI 味约束，场景本身有窗也不删。"""
    prompt = build_image_prompt(
        _slots(background="靠窗的书桌与窗外庭院")
    )

    assert "禁止出现窗户" in prompt


def test_blank_optional_slot_does_not_leave_dangling_punctuation():
    prompt = build_image_prompt(_slots(mood=""))

    assert "，，" not in prompt
    assert "整体氛围，" not in prompt


def test_prompt_is_single_line():
    """provider 侧按单行 prompt 处理，不要引入换行。"""
    prompt = build_image_prompt(_slots())

    assert "\n" not in prompt
