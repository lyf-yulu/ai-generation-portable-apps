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
