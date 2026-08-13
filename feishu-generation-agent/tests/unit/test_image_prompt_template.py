"""图片 prompt 必须按需求方给定的模板骨架来写。

模板（2026-08-13 由需求方给定）：
  禁止勾勒边缘线，画面风格严格参考图一重新生成图片，<景别>，<时间、场景>
  画面内容参考<人物自然融入场景>，<人物活动>，背景是<背景>。画面主次分明，
  <画风提示词>。画面主体控制在画布<宽>*<高>画面中央，光影自然，冷暖对比，
  光线暖柔，整体氛围<氛围>，整体画面高亮度高明度，画面风格严格参考图一生成
  图片，禁止勾勒边缘线，禁止勾勒边缘线，禁止拉伸图片，白天，禁止出现窗户

固定骨架每次都要出现；「白天」「禁止出现窗户」按文档实际时间与场景调整，
不能在夜景需求里硬写白天。
"""

from feishu_generation_agent.integrations.planner import (
    image_planner_system_prompt,
    planner_system_prompt,
)


def test_contract_carries_the_template_skeleton():
    prompt = image_planner_system_prompt()

    assert "禁止勾勒边缘线" in prompt
    assert "画面风格严格参考图一" in prompt
    assert "画面主次分明" in prompt
    assert "光影自然" in prompt
    assert "冷暖对比" in prompt
    assert "光线暖柔" in prompt
    assert "高亮度高明度" in prompt
    assert "禁止拉伸图片" in prompt


def test_contract_names_every_variable_slot():
    prompt = image_planner_system_prompt()

    for slot in ("景别", "时间", "场景", "人物活动", "背景", "氛围", "画风"):
        assert slot in prompt, f"模板槽位缺失：{slot}"


def test_contract_requires_subject_centered_in_canvas():
    prompt = image_planner_system_prompt()

    assert "画布" in prompt
    assert "画面中央" in prompt


def test_contract_requires_natural_integration_into_scene():
    prompt = image_planner_system_prompt()

    assert "融入" in prompt


def test_contract_makes_time_and_window_context_dependent():
    """「白天」「禁止出现窗户」要按文档实际情况写，不能一律硬套。"""
    prompt = image_planner_system_prompt()

    assert "窗户" in prompt
    assert "按文档" in prompt or "按需求" in prompt


def test_edge_line_ban_is_emphasised_by_repetition():
    """模板里「禁止勾勒边缘线」出现三次，是刻意强调，契约要保留这个写法。"""
    prompt = image_planner_system_prompt()

    assert prompt.count("禁止勾勒边缘线") >= 2


def test_video_contract_is_untouched_by_template():
    video = planner_system_prompt()

    assert "画面主次分明" not in video
    assert "高亮度高明度" not in video
