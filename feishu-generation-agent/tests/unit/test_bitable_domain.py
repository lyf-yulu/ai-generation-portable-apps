import pytest
from pydantic import ValidationError

from feishu_generation_agent.domain import (
    BitableBinding,
    BitableTaskSummary,
    TableTaskStatus,
)
from feishu_generation_agent.integrations.bitable_url import (
    parse_bitable_url,
    parse_requirement_source,
)


def test_parse_bitable_url_requires_matching_query_and_config():
    location = parse_bitable_url(
        "https://tenant.feishu.cn/wiki/wikiABC?table=tblABC&view=vewABC",
        table_id="tblABC",
        view_id="vewABC",
    )
    assert location.wiki_token == "wikiABC"
    assert location.table_id == "tblABC"
    assert location.view_id == "vewABC"

    with pytest.raises(ValueError, match="table"):
        parse_bitable_url(
            "https://tenant.feishu.cn/wiki/wikiABC?table=tblOTHER&view=vewABC",
            table_id="tblABC",
            view_id="vewABC",
        )


@pytest.mark.parametrize(
    ("url", "table_id", "view_id", "match"),
    [
        (
            "https://tenant.feishu.cn/wiki/wikiABC?view=vewABC",
            "tblABC",
            "vewABC",
            "table",
        ),
        (
            "https://tenant.feishu.cn/wiki/wikiABC?table=&view=vewABC",
            "tblABC",
            "vewABC",
            "table",
        ),
        (
            "https://tenant.feishu.cn/wiki/wikiABC?table=tblABC&table=tblABC&view=vewABC",
            "tblABC",
            "vewABC",
            "table",
        ),
        (
            "https://tenant.feishu.cn/wiki/wikiABC?table=tblOTHER&view=vewABC",
            "tblABC",
            "vewABC",
            "table",
        ),
        (
            "https://tenant.feishu.cn/wiki/wikiABC?table=tblABC",
            "tblABC",
            "vewABC",
            "view",
        ),
        (
            "https://tenant.feishu.cn/wiki/wikiABC?table=tblABC&view=",
            "tblABC",
            "vewABC",
            "view",
        ),
        (
            "https://tenant.feishu.cn/wiki/wikiABC?table=tblABC&view=vewABC&view=vewABC",
            "tblABC",
            "vewABC",
            "view",
        ),
        (
            "https://tenant.feishu.cn/wiki/wikiABC?table=tblABC&view=vewOTHER",
            "tblABC",
            "vewABC",
            "view",
        ),
    ],
)
def test_parse_bitable_url_rejects_invalid_table_or_view_query(
    url, table_id, view_id, match
):
    with pytest.raises(ValueError, match=match):
        parse_bitable_url(url, table_id=table_id, view_id=view_id)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (
            "https://tenant.feishu.cn/docx/docABC",
            "https://tenant.feishu.cn/docx/docABC",
        ),
        (
            {"link": "https://tenant.feishu.cn/wiki/wikiDOC", "text": "需求"},
            "https://tenant.feishu.cn/wiki/wikiDOC",
        ),
        (
            [{"link": "https://tenant.feishu.cn/docx/docABC"}],
            "https://tenant.feishu.cn/docx/docABC",
        ),
    ],
)
def test_parse_requirement_source_accepts_exactly_one_document(value, expected):
    assert parse_requirement_source(value) == expected


def test_parse_requirement_source_rejects_multiple_links():
    with pytest.raises(ValueError, match="恰好一个"):
        parse_requirement_source(
            [
                {"link": "https://tenant.feishu.cn/docx/a"},
                {"link": "https://tenant.feishu.cn/docx/b"},
            ]
        )


def test_parse_requirement_source_normalizes_and_deduplicates_document_links():
    source = parse_requirement_source(
        [
            "https://TENANT.FEISHU.CN./docx/docABC?from=bitable#section",
            {"link": "https://tenant.feishu.cn/docx/docABC"},
        ]
    )
    assert source == "https://tenant.feishu.cn/docx/docABC"


def test_parse_requirement_source_recurses_nested_lists_and_rich_text_links():
    source = parse_requirement_source(
        [[{"text": "需求", "link": [{"link": "https://tenant.feishu.cn/wiki/wikiABC"}]}]]
    )
    assert source == "https://tenant.feishu.cn/wiki/wikiABC"


def test_bitable_domain_contracts_have_safe_defaults_and_validation():
    first = BitableTaskSummary(
        record_id="rec-1",
        display_text="任务 1",
        source_url="https://tenant.feishu.cn/docx/doc-1",
    )
    second = BitableTaskSummary(
        record_id="rec-2",
        display_text="任务 2",
        source_url="https://tenant.feishu.cn/docx/doc-2",
    )

    assert first.status is TableTaskStatus.PENDING
    assert isinstance(first.record_id, str)
    assert isinstance(first.has_result, bool)
    first.executor_open_ids.append("ou_1")
    assert second.executor_open_ids == []

    binding = BitableBinding(
        app_token="app-1",
        table_id="tbl-1",
        view_id="vew-1",
        record_id="rec-1",
        source_url="https://tenant.feishu.cn/docx/doc-1",
        display_text="任务",
        run_id="run-1",
        thread_id="thread-1",
        claimant_open_id="ou_1",
        status=TableTaskStatus.PROCESSING,
    )
    another_binding = BitableBinding(
        app_token="app-2",
        table_id="tbl-2",
        view_id="vew-2",
        record_id="rec-2",
        source_url="https://tenant.feishu.cn/docx/doc-2",
        display_text="任务 2",
        run_id="run-2",
        thread_id="thread-2",
        claimant_open_id="ou_2",
        status=TableTaskStatus.PROCESSING,
    )
    binding.reply_context["message_id"] = "om_1"
    assert another_binding.reply_context == {}
    assert isinstance(binding.approval_version, int)

    with pytest.raises(ValidationError):
        BitableBinding(**(binding.model_dump() | {"approval_version": -1}))
