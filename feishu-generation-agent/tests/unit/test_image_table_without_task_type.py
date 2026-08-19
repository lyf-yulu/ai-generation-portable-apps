"""图片需求表是独立的另一张多维表格，且没有「需求类型」字段。

验证：schema 校验放行、扫描不被过滤掉、类型按来源补齐、交付白名单通过。
"""

from typing import Any

import pytest

from feishu_generation_agent.bitable.production_service import (
    ProductionTaskSource,
)
from feishu_generation_agent.domain.bitable import BitableLocation
from feishu_generation_agent.domain.production_bitable import (
    ProductionSourceSnapshot,
    ProductionTaskSummary,
)
from feishu_generation_agent.integrations.feishu_bitable import (
    BitableSchemaError,
)
from feishu_generation_agent.integrations.production_bitable import (
    ProductionBitableClient,
)


BASE_FIELDS = [
    {"field_name": "需求名称", "type": 1, "field_id": "fld_name"},
    {"field_name": "需求附件", "type": 15, "field_id": "fld_att"},
    {"field_name": "项目名称", "type": 4, "field_id": "fld_proj"},
    {"field_name": "发起人", "type": 11, "field_id": "fld_req"},
    {"field_name": "需求制作人", "type": 11, "field_id": "fld_maker"},
    {"field_name": "当前进度", "type": 3, "field_id": "fld_prog"},
]


class _Client:
    def __init__(self, fields: list[dict[str, Any]]) -> None:
        self.fields = fields

    async def iter_items(self, *_args: Any, **_kwargs: Any):
        return self.fields


def _location() -> BitableLocation:
    return BitableLocation(
        source_url="https://example.feishu.cn/wiki/tok?table=tbl1&view=vew1",
        wiki_token="tok",
        app_token="app1",
        table_id="tbl1",
        view_id="vew1",
    )


async def test_schema_accepts_table_without_task_type_field():
    client = ProductionBitableClient(_Client(BASE_FIELDS))

    schema = await client.ensure_schema(_location())

    assert schema.task_type_field_id == ""
    assert schema.requirement_name_field_id == "fld_name"


async def test_schema_still_accepts_table_with_task_type_field():
    client = ProductionBitableClient(
        _Client(
            BASE_FIELDS
            + [{"field_name": "需求类型", "type": 3, "field_id": "fld_type"}]
        )
    )

    schema = await client.ensure_schema(_location())

    assert schema.task_type_field_id == "fld_type"


async def test_schema_still_rejects_missing_required_field():
    client = ProductionBitableClient(
        _Client([f for f in BASE_FIELDS if f["field_name"] != "当前进度"])
    )

    with pytest.raises(BitableSchemaError):
        await client.ensure_schema(_location())


def test_image_source_matches_rows_with_blank_task_type():
    source = ProductionTaskSource(
        _location(),
        expected_task_type="",
        planning_mode="image",
        declared_task_type="图片类",
    )

    assert source.matches_task_type("") is True
    assert source.planning_mode == "image"


def test_video_sources_keep_filtering_by_task_type():
    source = ProductionTaskSource(_location(), expected_task_type="动画类")

    assert source.matches_task_type("动画类") is True
    assert source.matches_task_type("") is False
    assert source.declared_task_type == ""


def test_stamped_image_task_is_deliverable():
    """按来源补齐类型后，交付白名单与既有链路都能识别。"""
    summary = ProductionTaskSummary(
        record_id="rec-1",
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
    assert summary.delivery_block_reason is None
