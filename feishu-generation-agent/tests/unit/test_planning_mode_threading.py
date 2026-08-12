from types import SimpleNamespace
from typing import Any

from feishu_generation_agent.graph.nodes import (
    _planner_mode_argument,
    _planning_mode_for_run,
)


class _ModeAwarePlanner:
    async def plan(
        self,
        document: Any,
        visions: Any,
        feedback: Any = None,
        system_prompt: str | None = None,
        exact_system_prompt: str | None = None,
        mode: str = "video",
    ) -> Any:
        raise NotImplementedError


class _LegacyPlanner:
    """没有 mode 参数的老 planner（测试里的 fake 大多是这种）。"""

    async def plan(
        self,
        document: Any,
        visions: Any,
        feedback: Any = None,
        system_prompt: str | None = None,
    ) -> Any:
        raise NotImplementedError


def _store(task_type: str | None):
    if task_type is None:
        return None

    class Store:
        async def get_by_run(self, run_id: str):
            del run_id
            return SimpleNamespace(
                snapshot=SimpleNamespace(task_type=task_type)
            )

    return Store()


def test_mode_argument_is_passed_to_mode_aware_planner():
    assert _planner_mode_argument(_ModeAwarePlanner(), "image") == {
        "mode": "image"
    }


def test_mode_argument_is_omitted_for_legacy_planner():
    """老 planner 不接受 mode，必须省略而不是报 TypeError。"""
    assert _planner_mode_argument(_LegacyPlanner(), "image") == {}


def test_video_mode_argument_is_omitted_as_default():
    """video 是默认值，不必显式传，减少对存量 fake 的干扰。"""
    assert _planner_mode_argument(_ModeAwarePlanner(), "video") == {}


async def test_image_requirement_type_yields_image_mode():
    services = SimpleNamespace(production_task_store=_store("图片类"))

    assert await _planning_mode_for_run("run-1", services) == "image"


async def test_animation_requirement_type_yields_video_mode():
    services = SimpleNamespace(production_task_store=_store("动画类"))

    assert await _planning_mode_for_run("run-1", services) == "video"


async def test_real_person_requirement_type_yields_video_mode():
    services = SimpleNamespace(production_task_store=_store("真人类"))

    assert await _planning_mode_for_run("run-1", services) == "video"


async def test_legacy_run_without_production_store_yields_video_mode():
    services = SimpleNamespace(production_task_store=None)

    assert await _planning_mode_for_run("run-1", services) == "video"


async def test_missing_binding_yields_video_mode():
    class EmptyStore:
        async def get_by_run(self, run_id: str):
            del run_id
            return None

    services = SimpleNamespace(production_task_store=EmptyStore())

    assert await _planning_mode_for_run("run-1", services) == "video"


async def test_store_failure_falls_back_to_video_mode():
    """读取绑定失败不能让整个 run 挂掉，回落视频模式。"""

    class BrokenStore:
        async def get_by_run(self, run_id: str):
            del run_id
            raise RuntimeError("bitable unavailable")

    services = SimpleNamespace(production_task_store=BrokenStore())

    assert await _planning_mode_for_run("run-1", services) == "video"
