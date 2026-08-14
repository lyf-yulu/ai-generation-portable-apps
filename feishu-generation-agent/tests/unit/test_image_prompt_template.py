"""模板骨架由代码保证，契约只负责让模型填对槽位。

历史：这组测试原本断言模板骨架出现在契约文本里（方案 1：让模型照契约写）。
实测四轮下来模型时而套用、时而退回视频三段式，改为方案 2——骨架由
build_image_prompt 拼装，契约只说明槽位。因此断言对象从「契约文本」改为
「拼装结果 + 契约里的槽位说明」。
"""

from feishu_generation_agent.domain.image_prompt import (
    ImagePromptSlots,
    build_image_prompt,
)
from feishu_generation_agent.integrations.planner import (
    image_planner_system_prompt,
    planner_system_prompt,
)


def _prompt() -> str:
    return build_image_prompt(
        ImagePromptSlots(
            shot="中景",
            time_and_scene="白天，庄园大厅",
            subject_integration="@图片1 中的男性角色自然站在大厅中央",
            action="面部因愤怒而扭曲，双手握拳",
            background="庄园大厅的雕花立柱与帷幔",
            style="3D 卡通迪士尼风格，严格参考 @图片4、@图片5",
            canvas="1700*2500",
            mood="紧张压迫",
            time_of_day="白天",
        )
    )


def test_assembled_prompt_carries_the_template_skeleton():
    prompt = _prompt()

    for clause in (
        "禁止勾勒边缘线",
        "画面风格严格参考图一",
        "画面主次分明",
        "光影自然",
        "冷暖对比",
        "光线暖柔",
        "高亮度高明度",
        "禁止拉伸图片",
        "禁止出现窗户",
    ):
        assert clause in prompt, f"固定句式缺失：{clause}"


def test_contract_names_every_slot_field():
    """契约要逐一说明槽位字段，否则模型不知道填什么。"""
    contract = image_planner_system_prompt()

    for field in (
        "shot",
        "time_and_scene",
        "subject_integration",
        "action",
        "background",
        "style",
        "canvas",
        "mood",
        "time_of_day",
    ):
        assert field in contract, f"契约未说明槽位：{field}"


def test_assembled_prompt_centers_subject_in_canvas():
    prompt = _prompt()

    assert "画面主体控制在画布1700*2500画面中央" in prompt


def test_assembled_prompt_requires_natural_integration():
    prompt = _prompt()

    assert "画面内容参考" in prompt


def test_edge_line_ban_is_emphasised_by_repetition():
    """模板刻意重复三次强化约束。"""
    assert _prompt().count("禁止勾勒边缘线") == 3


def test_time_slot_follows_the_document():
    """时间按文档实际写，夜景不硬套白天。"""
    night = build_image_prompt(
        ImagePromptSlots(shot="近景", time_of_day="夜晚")
    )

    assert night.rstrip().endswith("夜晚，禁止出现窗户")


def test_window_ban_is_always_kept_as_anti_ai_constraint():
    """反 AI 味约束：模型爱堆窗户，堆多了一眼看出是 AI，始终保留。"""
    prompt = build_image_prompt(
        ImagePromptSlots(shot="全景", background="靠窗的书桌")
    )

    assert "禁止出现窗户" in prompt


def test_contract_tells_model_not_to_handwrite_the_template():
    contract = image_planner_system_prompt()

    assert "prompt_slots" in contract
    assert "不要在 prompt 里手写模板句式" in contract


def test_contract_requires_tokens_inside_slots():
    """@图片N 必须落在槽位里，否则参考图校验会判缺少引用。"""
    contract = image_planner_system_prompt()

    assert "@图片N" in contract


def test_video_contract_is_untouched_by_template():
    video = planner_system_prompt()

    assert "画面主次分明" not in video
    assert "高亮度高明度" not in video
    assert "prompt_slots" not in video
