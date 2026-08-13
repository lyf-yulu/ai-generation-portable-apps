"""图片契约必须交代三类参考图，否则模型会把它们当无用素材排除掉。

真实故障：拿新规范文档跑出的计划里，画风参考和场景参考全被丢进
excluded_assets。原因是契约只讲了「角色参考」，从没提「风格参考」
和「场景参考」这两节，模型不知道它们该挂上。

规范文档结构（2026-08-13 版）：
  二、需求登场角色及角色状态  → 服装参考，仅参考服装不参考画风
  三、场景参考                → 同一场景多角度，用于统一空间一致性
  三、风格参考                → 色调与画风统一
  四、具体需求                → 编号 / 内容描述 / 对应场景
"""

from feishu_generation_agent.integrations.planner import (
    image_planner_system_prompt,
    planner_system_prompt,
)


def test_contract_covers_style_reference():
    prompt = image_planner_system_prompt()

    assert "风格参考" in prompt
    assert "色调" in prompt


def test_contract_covers_scene_reference():
    prompt = image_planner_system_prompt()

    assert "场景参考" in prompt
    assert "对应场景" in prompt


def test_contract_covers_character_reference_clothing_only():
    """规范明确写了「仅参考服装，不能参考画风」，契约必须传达这条。"""
    prompt = image_planner_system_prompt()

    assert "服装" in prompt


def test_contract_forbids_discarding_style_and_scene_refs():
    prompt = image_planner_system_prompt()

    assert "excluded_assets" in prompt


def test_contract_covers_concept_sketch_reference():
    """「概念/豆包/火柴人」是制作人为提升模型理解力做的构图示意图。

    有图时约束力高于风格参考，必须挂上。
    """
    prompt = image_planner_system_prompt()

    assert "概念" in prompt
    assert "火柴人" in prompt
    assert "构图" in prompt


def test_contract_makes_concept_sketch_conditional():
    """该列不一定每次都有图；没有时不得虚构，跳过即可。"""
    prompt = image_planner_system_prompt()

    assert "没有图" in prompt


def test_contract_treats_safe_area_as_boundary_not_output_size():
    """出图按完整版尺寸一张；安全区是构图界限，不是第二个交付尺寸。"""
    prompt = image_planner_system_prompt()

    assert "完整版" in prompt
    assert "安全区" in prompt
    assert "不是交付尺寸" in prompt


def test_contract_still_explains_required_fields():
    prompt = image_planner_system_prompt()

    assert "image_size" in prompt
    assert "size_variants" in prompt
    assert "image_provider" in prompt


def test_video_contract_is_untouched():
    """图片契约的改动不得影响视频契约。"""
    video = planner_system_prompt()

    assert "风格参考" not in video
    assert "场景参考" not in video
