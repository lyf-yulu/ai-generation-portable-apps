"""图片模式的用户提示词里不能混入 Seedance 视频指令。

真实故障：契约里的图片模板明明写了固定骨架（禁止勾勒边缘线、画面主次分明、
高亮度高明度…），但实际产出的 prompt 一句都没有，反而带着「无水印、无 Logo」
这类 Seedance 专属约束。

原因是 _planning_prompt 无条件发送视频指令，而用户提示词离生成更近，冲突时
会压过 system prompt 里的图片模板。
"""

from pathlib import Path

from feishu_generation_agent.domain.document import (
    DocumentBlock,
    NormalizedDocument,
    SourceType,
)
from feishu_generation_agent.integrations.planner import DeepSeekPlanner


def _document() -> NormalizedDocument:
    return NormalizedDocument(
        document_id="doc-1",
        title="CG 需求",
        revision=1,
        source_type=SourceType.WIKI,
        source_token="tok",
        blocks=[
            DocumentBlock(
                block_id="b1",
                parent_id=None,
                block_type="text",
                order=0,
                path=["b1"],
                text="编号 1：Victor 中景，愤怒",
            )
        ],
        text_view="[block:b1] 编号 1：Victor 中景，愤怒",
        media_assets=[],
    )


class _Model:
    def bind(self, **_kwargs):
        return self


def _prompt(mode: str) -> str:
    planner = DeepSeekPlanner(_Model())
    return planner._planning_prompt(_document(), [], None, mode=mode)


def _instructions(mode: str) -> str:
    """只取指令段：JSON Schema 里含 TaskType 枚举等合法字面量，不该参与断言。"""
    prompt = _prompt(mode)
    return prompt.split("TaskPlan JSON Schema=")[0]


def test_image_mode_drops_seedance_instructions():
    instructions = _instructions("image")

    assert "Seedance" not in instructions
    assert "分镜" not in instructions
    assert "generate_audio" not in instructions
    assert "秒数" not in instructions


def test_video_mode_keeps_seedance_instructions():
    prompt = _prompt("video")

    assert "Seedance" in prompt
    assert "分镜" in prompt


def test_image_mode_only_allows_image_task_type():
    instructions = _instructions("image")

    assert "image_to_image" in instructions
    assert "image_to_video" not in instructions


def test_video_mode_still_allows_both_task_types():
    prompt = _prompt("video")

    assert "image_to_image" in prompt
    assert "image_to_video" in prompt


def test_image_mode_defers_prompt_format_to_contract():
    """图片模式不该在用户提示词里另立 prompt 格式，避免与模板冲突。"""
    instructions = _instructions("image")

    assert "无水印" not in instructions
    assert "无 Logo" not in instructions


def test_both_modes_keep_asset_accounting_rules():
    """素材必须归入 reference_images 或 excluded_assets——两个模式都要。"""
    for mode in ("image", "video"):
        prompt = _prompt(mode)
        assert "reference_images" in prompt
        assert "excluded_assets" in prompt


def test_default_mode_is_video_for_backward_compatibility():
    planner = DeepSeekPlanner(_Model())

    prompt = planner._planning_prompt(_document(), [], None)

    assert "Seedance" in prompt
