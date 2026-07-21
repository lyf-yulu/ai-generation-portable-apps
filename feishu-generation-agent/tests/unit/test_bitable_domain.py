import pytest

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
