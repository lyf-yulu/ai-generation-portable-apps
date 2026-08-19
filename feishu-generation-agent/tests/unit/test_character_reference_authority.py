"""角色参考图是主体依据，不是只看衣服。

真实故障：产出的 prompt 写成「参考 @图片1 中角色身穿灰色西装套装…作为
Victor 的服装参考」，把角色参考的约束力缩到只剩服装。模型于是可以自己编
一张脸，角色一致性直接失效。

根源是契约照搬了规范文档那句「仅能参考服装，不能参考画风」。那句的本意是
「别抄画风」，不是「除了衣服什么都别看」——五官、发型、身形都必须沿用。
"""

from feishu_generation_agent.integrations.planner import (
    image_planner_system_prompt,
)


def test_character_reference_covers_identity_not_only_clothing():
    prompt = image_planner_system_prompt()

    for token in ("五官", "发型", "身形"):
        assert token in prompt, f"角色参考必须覆盖 {token}"


def test_character_reference_still_excludes_art_style():
    """「不参考画风」这条要保留——画风统一由风格参考图负责。"""
    prompt = image_planner_system_prompt()

    assert "不参考画风" in prompt or "不要参考画风" in prompt


def test_contract_forbids_reducing_character_ref_to_clothing():
    prompt = image_planner_system_prompt()

    assert "服装参考" in prompt


def test_contract_requires_identity_consistency_wording():
    """prompt 里要明确写「沿用」，而不是含糊的「参考」。"""
    prompt = image_planner_system_prompt()

    assert "沿用" in prompt
