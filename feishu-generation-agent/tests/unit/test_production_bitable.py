from feishu_generation_agent.config import Settings
from feishu_generation_agent import domain
from feishu_generation_agent.domain.production_bitable import (
    ProductionSourceSnapshot,
    ProductionTaskSummary,
)
from feishu_generation_agent.integrations.production_bitable import (
    ProductionBitableClient,
)
from feishu_generation_agent.domain.bitable import BitableLocation


def test_production_settings_require_source_and_result_folder() -> None:
    settings = Settings(
        _env_file=None,
        lark_production_bitable_url="https://tenant.feishu.cn/wiki/wikiProd",
        lark_production_table_id="tblProd",
        lark_production_view_id="vewProd",
        lark_result_folder_token="fldResults",
    )

    assert hasattr(settings, "production_bitable_configured")
    assert settings.production_bitable_configured is True
    assert settings.lark_include_completed_for_test is False


def test_exports_production_task_models() -> None:
    assert hasattr(domain, "ProductionSourceSnapshot")
    assert hasattr(domain, "ProductionTaskSummary")


def test_production_task_without_maker_is_readable_but_not_deliverable() -> None:
    task = ProductionTaskSummary(
        record_id="rec1",
        display_text="需求 A",
        source_url="https://tenant.feishu.cn/docx/doc1",
        progress="未开始",
        snapshot=ProductionSourceSnapshot(
            requirement_name="需求 A",
            requirement_attachment="https://tenant.feishu.cn/docx/doc1",
        ),
    )

    payload = task.model_dump()
    assert payload.get("deliverable") is False
    assert payload.get("delivery_block_reason") == "缺少需求制作人"


async def test_lists_readable_production_tasks_and_filters_completed() -> None:
    fields = [
        {"field_id": "fld_name", "field_name": "需求名称", "type": 1},
        {"field_id": "fld_attachment", "field_name": "需求附件", "type": 1},
        {"field_id": "fld_project", "field_name": "项目名称", "type": 4},
        {"field_id": "fld_requester", "field_name": "发起人", "type": 11},
        {"field_id": "fld_maker", "field_name": "需求制作人", "type": 11},
        {"field_id": "fld_progress", "field_name": "当前进度", "type": 3},
    ]
    records = [
        {
            "record_id": "rec-new",
            "fields": {
                "需求名称": "需求 A",
                "需求附件": "https://tenant.feishu.cn/docx/docA",
                "项目名称": ["项目 A"],
                "发起人": [{"id": "ou-request", "name": "发起人"}],
                "需求制作人": [{"id": "ou-maker", "name": "制作人"}],
                "当前进度": "未开始",
            },
        },
        {
            "record_id": "rec-done",
            "fields": {
                "需求名称": "已完成需求",
                "需求附件": "https://tenant.feishu.cn/wiki/wikiDone",
                "项目名称": [],
                "发起人": [],
                "需求制作人": [],
                "当前进度": "已确认完成",
            },
        },
        {
            "record_id": "rec-invalid",
            "fields": {
                "需求名称": "不可读需求",
                "需求附件": "仅文字说明",
                "项目名称": [],
                "发起人": [],
                "需求制作人": [],
                "当前进度": "未开始",
            },
        },
    ]

    class FakeClient:
        async def iter_items(self, path: str, *, params=None):
            return records if path.endswith("/records") else fields

    location = BitableLocation(
        wiki_token="wikiProd",
        app_token="appProd",
        table_id="tblProd",
        view_id="vewProd",
        source_url="https://tenant.feishu.cn/wiki/wikiProd?table=tblProd&view=vewProd",
    )
    client = ProductionBitableClient(FakeClient())
    schema = await client.ensure_schema(location)

    normal = await client.list_tasks(location, schema, include_completed=False)
    test_mode = await client.list_tasks(location, schema, include_completed=True)

    assert [task.record_id for task in normal] == ["rec-new"]
    assert [task.record_id for task in test_mode] == ["rec-new", "rec-done"]
    assert normal[0].maker_open_id == "ou-maker"
    assert normal[0].snapshot.project_names == ["项目 A"]
