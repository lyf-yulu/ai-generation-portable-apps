"""直连文档创建 run：输入具体需求文档链接，输出可修改的计划。

这条路径不经过多维表格，没有 binding，所以模式判定不能只看 binding，
必须支持在创建时直接声明。
"""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from feishu_generation_agent.domain.document import RequirementRequest
from feishu_generation_agent.graph.nodes import _planning_mode_for_run
from feishu_generation_agent.web.schemas import CreateRunRequest


WIKI_URL = "https://redcqchina.feishu.cn/wiki/BOlPwJ3I7iBpxLkHbKcc1fY6nmh"


def test_create_run_defaults_to_video_mode():
    request = CreateRunRequest.model_validate({"source_url": WIKI_URL})

    assert request.planning_mode == "video"
    assert request.to_domain().planning_mode == "video"


def test_create_run_accepts_image_mode():
    request = CreateRunRequest.model_validate(
        {"source_url": WIKI_URL, "planning_mode": "image"}
    )

    assert request.to_domain().planning_mode == "image"


def test_create_run_rejects_unknown_mode():
    with pytest.raises(ValidationError):
        CreateRunRequest.model_validate(
            {"source_url": WIKI_URL, "planning_mode": "audio"}
        )


def test_requirement_request_defaults_to_video():
    assert RequirementRequest(source_url=WIKI_URL).planning_mode == "video"


async def test_state_planning_mode_wins_for_direct_runs():
    """直连 run 没有 binding，模式来自 state。"""
    services = SimpleNamespace(production_task_store=None)

    mode = await _planning_mode_for_run(
        "run-1", services, state={"planning_mode": "image"}
    )

    assert mode == "image"


async def test_direct_run_without_mode_stays_video():
    services = SimpleNamespace(production_task_store=None)

    mode = await _planning_mode_for_run("run-1", services, state={})

    assert mode == "video"


async def test_binding_still_wins_when_state_has_no_mode():
    """多维表格来的 run 行为不变。"""

    class Store:
        async def get_by_run(self, run_id: str):
            del run_id
            return SimpleNamespace(
                planning_mode="image",
                snapshot=SimpleNamespace(task_type="图片类"),
            )

    services = SimpleNamespace(production_task_store=Store())

    assert await _planning_mode_for_run("run-1", services, state={}) == "image"


async def test_state_mode_takes_priority_over_binding():
    """人工在界面上改过模式时，以显式声明为准。"""

    class Store:
        async def get_by_run(self, run_id: str):
            del run_id
            return SimpleNamespace(
                planning_mode="video",
                snapshot=SimpleNamespace(task_type="动画类"),
            )

    services = SimpleNamespace(production_task_store=Store())

    mode = await _planning_mode_for_run(
        "run-1", services, state={"planning_mode": "image"}
    )

    assert mode == "image"


async def test_missing_state_argument_is_backward_compatible():
    services = SimpleNamespace(production_task_store=None)

    assert await _planning_mode_for_run("run-1", services) == "video"
