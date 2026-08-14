"""审计报告超长不该让整个 run 失败。

真实故障：图片计划出了 4 个任务后 run 挂在审计节点——
  operation=audit cause=LengthFinishReasonError status=200
审计模型输出被截断，抛 LengthFinishReasonError。计划本身已经生成好了，
却因为「审查意见写太长」把整个 run 判失败。

审计是辅助环节（给人看的风险提示），不该有一票否决权。截断时按「无额外
发现」处理并记 warning，让计划继续走到人工审批。
"""

import copy
from types import SimpleNamespace
from typing import Any

from feishu_generation_agent.domain.document import (
    DocumentBlock,
    NormalizedDocument,
    SourceType,
)
from feishu_generation_agent.domain.plan import TaskPlan
from feishu_generation_agent.integrations.planner import DeepSeekPlanner


class _LengthError(Exception):
    """模拟 openai.LengthFinishReasonError。"""


class _TruncatingModel:
    def __init__(self) -> None:
        self.calls = 0

    def bind(self, **_kwargs: Any) -> "_TruncatingModel":
        return self

    async def ainvoke(self, messages: list[dict[str, Any]], config=None):
        del messages, config
        self.calls += 1
        raise _LengthError("Could not parse response content as the length limit was reached")


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
                text="编号 1：Victor 中景",
            )
        ],
        text_view="[block:b1] 编号 1：Victor 中景",
        media_assets=[],
    )


def _plan() -> TaskPlan:
    return TaskPlan.model_validate(
        {
            "document_summary": "CG 需求",
            "excluded_assets": [],
            "tasks": [
                {
                    "task_id": "cg-1",
                    "task_type": "image_to_image",
                    "title": "Victor 中景",
                    "source_block_ids": ["b1"],
                    "user_intent": "出 CG 图",
                    "prompt": "景别：中景；动作：握拳；背景：帷幔",
                    "reference_images": [
                        {
                            "asset_id": "a1",
                            "role": "reference_image",
                            "order": 1,
                        }
                    ],
                    "aspect_ratio": "9:16",
                    "image_size": "2K",
                }
            ],
        }
    )


async def test_truncated_audit_does_not_fail_the_run():
    model = _TruncatingModel()
    planner = DeepSeekPlanner(model)

    report = await planner.audit(_document(), _plan())

    assert report is not None, "审计截断不该让整个 run 失败"


async def test_truncated_audit_reports_a_warning():
    model = _TruncatingModel()
    planner = DeepSeekPlanner(model)

    report = await planner.audit(_document(), _plan())

    text = " ".join(report.issues)
    assert "审计" in text, "必须留下痕迹，否则人工不知道审计没跑完"


async def test_truncated_audit_does_not_block_approval():
    model = _TruncatingModel()
    planner = DeepSeekPlanner(model)

    report = await planner.audit(_document(), _plan())

    assert report.corrections_required is False, "审计截断不是技术阻断"
