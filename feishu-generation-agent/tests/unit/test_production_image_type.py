from feishu_generation_agent.domain.production_bitable import (
    ProductionSourceSnapshot,
    ProductionTaskSummary,
)


def _summary(task_type: str) -> ProductionTaskSummary:
    return ProductionTaskSummary(
        record_id="rec-1",
        display_text="女儿穿越救母 day6 CG 图需求",
        source_url="https://example.feishu.cn/wiki/token-cg",
        progress="待制作",
        task_type=task_type,
        snapshot=ProductionSourceSnapshot(
            requirement_name="女儿穿越救母 day6 CG 图需求",
            task_type=task_type,
            requirement_attachment="https://example.feishu.cn/wiki/token-cg",
        ),
    )


def test_image_type_is_deliverable():
    summary = _summary("图片类")

    assert summary.deliverable is True
    assert summary.delivery_block_reason is None


def test_animation_and_portrait_stay_deliverable():
    for task_type in ("动画类", "真人类"):
        assert _summary(task_type).deliverable is True


def test_unknown_type_is_still_blocked():
    summary = _summary("配音类")

    assert summary.deliverable is False
    assert summary.delivery_block_reason == "配音类任务暂未启用"


def test_blank_type_reports_uncategorised():
    summary = _summary("")

    assert summary.deliverable is False
    assert summary.delivery_block_reason == "未分类任务暂未启用"


def test_image_type_reuses_same_snapshot_shape():
    """图片类走同一张生产表、同一套 snapshot 字段，不新建交付目标。"""
    summary = _summary("图片类")

    assert summary.snapshot.task_type == "图片类"
    assert summary.source_url.startswith("https://")
