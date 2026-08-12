"""阶段 2 端到端：图片类需求 → 图片契约 → provider 路由 → 多产出 → 尺寸变体。

各 Task 的单测都过了，这里验证它们真能串成一条链，而不是各自为政。
"""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from PIL import Image

from feishu_generation_agent.domain.plan import GenerationTask, TaskPlan
from feishu_generation_agent.domain.production_bitable import (
    ProductionSourceSnapshot,
    ProductionTaskSummary,
)
from feishu_generation_agent.graph.nodes import (
    _execution_units,
    _generator_for_task,
    _planner_mode_argument,
    _planning_mode_for_run,
)
from feishu_generation_agent.integrations.image_resize import (
    render_size_variants,
)
from feishu_generation_agent.integrations.planner import (
    image_planner_system_prompt,
    planner_system_prompt,
)


def _cg_plan_payload() -> dict[str, Any]:
    """模拟 planner 在图片模式下产出的计划：两个概念，各自双尺寸。"""
    return {
        "document_summary": "女儿穿越救母 day6 CG 图需求",
        "excluded_assets": [],
        "tasks": [
            {
                "task_id": "cg-1",
                "task_type": "image_to_image",
                "title": "Victor 中景",
                "source_block_ids": ["cg-1"],
                "user_intent": "生成 Victor 愤怒表情的 CG 插图",
                "prompt": (
                    "@图片1 中的男性角色中景，脸部因愤怒而扭曲，"
                    "戏剧化顶光 + 侧逆光，3D 卡通迪士尼风格"
                ),
                "negative_constraints": ["禁止勾勒黑色边缘线", "禁止拉伸图片"],
                "reference_images": [
                    {
                        "asset_id": "image-1",
                        "role": "reference_image",
                        "order": 1,
                    }
                ],
                "aspect_ratio": "9:16",
                "image_size": "2K",
                "image_provider": "banana",
                "size_variants": ["1080x2080", "1700x2500"],
                "safe_area": "1080x2080",
                "output_count": 1,
                "confidence": 0.9,
            },
            {
                "task_id": "cg-2",
                "task_type": "image_to_image",
                "title": "Sophia 与 Sarah 握手言和",
                "source_block_ids": ["cg-2"],
                "user_intent": "生成两位女性和解的 CG 插图",
                "prompt": (
                    "@图片1 中的女性角色与另一位女性握手言和，"
                    "明媚的光线，两人都散发自信"
                ),
                "negative_constraints": [],
                "reference_images": [
                    {
                        "asset_id": "image-1",
                        "role": "reference_image",
                        "order": 1,
                    }
                ],
                "aspect_ratio": "9:16",
                "image_size": "2K",
                "image_provider": "seedream",
                "size_variants": ["1080x2080", "1700x2500"],
                "output_count": 2,
                "confidence": 0.85,
            },
        ],
    }


def _store(task_type: str):
    class Store:
        async def get_by_run(self, run_id: str):
            del run_id
            return SimpleNamespace(
                snapshot=SimpleNamespace(task_type=task_type)
            )

    return Store()


async def test_image_requirement_flows_end_to_end(tmp_path: Path):
    # 1. 生产表「图片类」→ 生产表放行
    summary = ProductionTaskSummary(
        record_id="rec-cg",
        display_text="女儿穿越救母 day6 CG 图需求",
        source_url="https://example.feishu.cn/wiki/token-cg",
        progress="待制作",
        task_type="图片类",
        snapshot=ProductionSourceSnapshot(
            requirement_name="女儿穿越救母 day6 CG 图需求",
            task_type="图片类",
            requirement_attachment="https://example.feishu.cn/wiki/token-cg",
        ),
    )
    assert summary.deliverable is True

    # 2. 需求类型 → 规划模式 image，并真的传给支持 mode 的 planner
    services = SimpleNamespace(production_task_store=_store("图片类"))
    mode = await _planning_mode_for_run("run-cg", services)
    assert mode == "image"

    class ModeAwarePlanner:
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

    assert _planner_mode_argument(ModeAwarePlanner(), mode) == {"mode": "image"}
    # 图片模式用的是图片契约，不是视频契约
    assert image_planner_system_prompt() != planner_system_prompt()

    # 3. 计划能被领域模型接受，图片专属字段完整保留
    plan = TaskPlan.model_validate(_cg_plan_payload())
    assert [task.task_id for task in plan.tasks] == ["cg-1", "cg-2"]
    assert plan.tasks[0].size_variants == ["1080x2080", "1700x2500"]
    assert plan.tasks[0].safe_area == "1080x2080"

    # 4. provider 按 task 各自路由
    registry = {
        "banana": "banana-generator",
        "seedream": "seedream-generator",
        "gpt-image2": "gpt-generator",
    }
    routed = SimpleNamespace(
        image_providers=registry,
        image_generator="legacy",
        video_generator="seedance",
        portrait_video_generator=None,
        production_task_store=None,
    )
    assert await _generator_for_task("run-cg", plan.tasks[0], routed) == (
        "banana",
        "banana-generator",
    )
    assert await _generator_for_task("run-cg", plan.tasks[1], routed) == (
        "seedream",
        "seedream-generator",
    )

    # 5. 多产出拆分：cg-1 单张不拆，cg-2 两张拆成两个执行单元
    assert len(_execution_units(plan.tasks[0])) == 1
    units = _execution_units(plan.tasks[1])
    assert [unit.task_id for unit in units] == [
        "cg-2::output:1",
        "cg-2::output:2",
    ]
    # 拆分后仍保留 provider 与尺寸要求
    assert all(unit.resolved_image_provider == "seedream" for unit in units)

    # 6. 出图后按 size_variants 渲染双尺寸
    generated = tmp_path / "cg-1.png"
    Image.new("RGB", (2048, 2048), (30, 40, 90)).save(generated)
    rendered = render_size_variants(
        generated,
        plan.tasks[0].size_variants,
        output_dir=tmp_path / "variants",
    )
    assert [item.variant for item in rendered] == ["1080x2080", "1700x2500"]
    for item in rendered:
        with Image.open(item.path) as image:
            assert f"{image.width}x{image.height}" == item.variant


async def test_video_requirement_is_unaffected(tmp_path: Path):
    """同一条链路上，动画类需求行为与图片模式接入前一致。"""
    services = SimpleNamespace(production_task_store=_store("动画类"))

    assert await _planning_mode_for_run("run-anim", services) == "video"

    class ModeAwarePlanner:
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

    # video 是默认值，不显式传，存量 fake planner 不受影响
    assert _planner_mode_argument(ModeAwarePlanner(), "video") == {}

    video = GenerationTask.model_validate(
        {
            "task_id": "anim-1",
            "task_type": "image_to_video",
            "title": "熊猫拉抽屉",
            "source_block_ids": ["block-1"],
            "user_intent": "完成动作",
            "prompt": "熊猫拉开抽屉，彩球滚出",
            "reference_images": [
                {"asset_id": "asset-1", "role": "reference_image", "order": 1}
            ],
            "aspect_ratio": "9:16",
            "duration": 5,
            "resolution": "720p",
        }
    )
    routed = SimpleNamespace(
        image_providers={"banana": "banana-generator"},
        image_generator="legacy",
        video_generator="seedance-generator",
        portrait_video_generator=None,
        production_task_store=None,
    )

    assert await _generator_for_task("run-anim", video, routed) == (
        "seedance",
        "seedance-generator",
    )
    assert video.resolved_image_provider is None
    assert video.size_variants == []
