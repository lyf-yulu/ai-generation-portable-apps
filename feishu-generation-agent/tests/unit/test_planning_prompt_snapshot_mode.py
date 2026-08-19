"""直连 run 的 prompt 快照必须按 mode 选契约。

真实故障：图片模式改了几轮契约都不生效，产出的 prompt 始终是视频的
「总体设定与素材绑定 → 镜头描述 → 风格与约束」三段式。

根因不在契约，而在优先级：直连 run 走 prime-local 分支，把快照通过
exact_system_prompt 传给 planner，而该分支在 plan() 里排在 image_mode
之前，直接压过图片契约。快照内容又固定取 planner_system_prompt()——
视频契约。于是图片契约永远拿不到执行机会。
"""

from feishu_generation_agent.domain.document import (
    build_planning_prompt_snapshot,
)
from feishu_generation_agent.graph.nodes import _planning_prompt
from feishu_generation_agent.integrations.planner import (
    image_planner_system_prompt,
    planner_system_prompt,
)


def test_image_mode_snapshot_uses_image_contract():
    snapshot = _planning_prompt({"planning_mode": "image"})

    assert snapshot.prompt_text == image_planner_system_prompt()
    assert "禁止勾勒边缘线" in snapshot.prompt_text


def test_video_mode_snapshot_uses_video_contract():
    snapshot = _planning_prompt({"planning_mode": "video"})

    assert snapshot.prompt_text == planner_system_prompt()


def test_missing_mode_defaults_to_video():
    """存量 run 没有 planning_mode 字段，必须保持原行为。"""
    snapshot = _planning_prompt({})

    assert snapshot.prompt_text == planner_system_prompt()


def test_explicit_snapshot_is_respected():
    """人工在界面上改过 prompt 时，以该快照为准，不被 mode 覆盖。"""
    custom = build_planning_prompt_snapshot(
        owner_user_id="user-a",
        source="personal",
        version=3,
        prompt_text="自定义业务提示词",
    )

    snapshot = _planning_prompt(
        {
            "planning_mode": "image",
            "planning_prompt": custom.model_dump(mode="json"),
        }
    )

    assert snapshot.prompt_text == "自定义业务提示词"


def test_image_snapshot_carries_slot_contract():
    """快照必须带槽位契约——模板骨架已移到代码层拼装。"""
    snapshot = _planning_prompt({"planning_mode": "image"})

    assert "prompt_slots" in snapshot.prompt_text
    for field in ("shot", "subject_integration", "style", "canvas"):
        assert field in snapshot.prompt_text
