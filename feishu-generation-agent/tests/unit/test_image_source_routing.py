"""图片类需求来自另一张多维表格，且那张表没有「需求类型」字段。

因此模式判定不能靠字段值，必须靠「这条 run 来自哪个 source」。
"""

from types import SimpleNamespace

from feishu_generation_agent.bitable.production_service import (
    ProductionTaskSource,
)
from feishu_generation_agent.domain.bitable import BitableLocation
from feishu_generation_agent.graph.nodes import _planning_mode_for_run


def _location(view: str = "vew1") -> BitableLocation:
    return BitableLocation(
        source_url=f"https://example.feishu.cn/wiki/tok?table=tbl1&view={view}",
        wiki_token="tok",
        app_token="app1",
        table_id="tbl1",
        view_id=view,
    )


def test_source_defaults_to_video_mode():
    source = ProductionTaskSource(_location(), expected_task_type="动画类")

    assert source.planning_mode == "video"


def test_source_can_declare_image_mode():
    source = ProductionTaskSource(
        _location(),
        expected_task_type="",
        planning_mode="image",
    )

    assert source.planning_mode == "image"


def test_source_without_task_type_field_matches_every_row():
    """图片表没有「需求类型」字段，行上的 task_type 恒为空。

    这种表必须把 expected_task_type 留空，表示「本表所有行都算数」。
    """
    source = ProductionTaskSource(
        _location(), expected_task_type="", planning_mode="image"
    )

    assert source.matches_task_type("") is True
    assert source.matches_task_type("随便什么") is True


def test_source_with_task_type_field_still_filters():
    source = ProductionTaskSource(_location(), expected_task_type="动画类")

    assert source.matches_task_type("动画类") is True
    assert source.matches_task_type("真人类") is False


def _binding(planning_mode: str | None, task_type: str = ""):
    snapshot = SimpleNamespace(task_type=task_type)
    if planning_mode is None:
        return SimpleNamespace(snapshot=snapshot)
    return SimpleNamespace(snapshot=snapshot, planning_mode=planning_mode)


def _services(binding):
    class Store:
        async def get_by_run(self, run_id: str):
            del run_id
            return binding

    return SimpleNamespace(production_task_store=Store())


async def test_binding_planning_mode_drives_image_mode():
    """图片表的 run 没有需求类型字段，靠 binding 上记录的模式判定。"""
    services = _services(_binding("image"))

    assert await _planning_mode_for_run("run-1", services) == "image"


async def test_binding_planning_mode_video_stays_video():
    services = _services(_binding("video", task_type="动画类"))

    assert await _planning_mode_for_run("run-1", services) == "video"


async def test_legacy_binding_without_planning_mode_falls_back_to_task_type():
    """存量 binding 没有 planning_mode 字段，仍按需求类型判定。"""
    services = _services(_binding(None, task_type="图片类"))

    assert await _planning_mode_for_run("run-1", services) == "image"


async def test_legacy_binding_animation_stays_video():
    services = _services(_binding(None, task_type="动画类"))

    assert await _planning_mode_for_run("run-1", services) == "video"
