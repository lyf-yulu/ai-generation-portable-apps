from feishu_generation_agent.domain.bitable import BitableLocation
from feishu_generation_agent.integrations.bitable_url import with_bitable_view


def test_with_bitable_view_preserves_table_and_replaces_view() -> None:
    source = BitableLocation(
        wiki_token="wikiProd",
        table_id="tblProd",
        view_id="vewAnimation",
        source_url=(
            "https://tenant.feishu.cn/wiki/wikiProd"
            "?table=tblProd&view=vewAnimation"
        ),
    )

    portrait = with_bitable_view(source, "vewPortrait")

    assert portrait.wiki_token == "wikiProd"
    assert portrait.table_id == "tblProd"
    assert portrait.view_id == "vewPortrait"
    assert "table=tblProd" in portrait.source_url
    assert "view=vewPortrait" in portrait.source_url


def test_with_bitable_view_preserves_repeated_blank_and_fragment_parameters() -> None:
    source = BitableLocation(
        wiki_token="wikiProd",
        table_id="tblProd",
        view_id="vewAnimation",
        source_url=(
            "https://tenant.feishu.cn/wiki/wikiProd"
            "?tag=first&table=stale&tag=second&empty=&view=vewAnimation"
            "#task-panel"
        ),
    )
    original = source.model_copy(deep=True)

    portrait = with_bitable_view(source, "vewPortrait")

    assert portrait.source_url == (
        "https://tenant.feishu.cn/wiki/wikiProd"
        "?tag=first&tag=second&empty=&table=tblProd&view=vewPortrait"
        "#task-panel"
    )
    assert source == original
