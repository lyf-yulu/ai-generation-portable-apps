from feishu_generation_agent.config import Settings
from feishu_generation_agent import domain
from feishu_generation_agent.domain.production_bitable import (
    ProductionSourceSnapshot,
    ProductionTaskSummary,
)


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
