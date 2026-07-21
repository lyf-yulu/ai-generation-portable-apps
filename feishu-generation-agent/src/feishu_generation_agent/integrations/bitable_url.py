from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qs, urlsplit, urlunsplit

from feishu_generation_agent.domain.bitable import BitableLocation
from feishu_generation_agent.domain.document import SourceType
from feishu_generation_agent.integrations.feishu_source import parse_feishu_url


def parse_bitable_url(url: str, table_id: str, view_id: str) -> BitableLocation:
    source_type, wiki_token = parse_feishu_url(url)
    if source_type is not SourceType.WIKI:
        raise ValueError("多维表格链接必须是 wiki 链接")

    query = parse_qs(urlsplit(url).query, keep_blank_values=True)
    _require_matching_query_value(query, "table", table_id)
    _require_matching_query_value(query, "view", view_id)

    return BitableLocation(
        wiki_token=wiki_token,
        table_id=table_id,
        view_id=view_id,
        source_url=url,
    )


def parse_requirement_source(value: Any) -> str:
    sources = {
        _normalize_document_url(candidate)
        for candidate in _iter_source_candidates(value)
    }
    if len(sources) != 1:
        raise ValueError("需求来源必须恰好一个飞书文档链接")
    return sources.pop()


def _require_matching_query_value(
    query: dict[str, list[str]], name: str, expected: str
) -> None:
    if not expected or query.get(name) != [expected]:
        raise ValueError(f"链接中的 {name} 必须与配置一致")


def _iter_source_candidates(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        if "link" in value:
            yield from _iter_source_candidates(value["link"])
    elif isinstance(value, list):
        for item in value:
            yield from _iter_source_candidates(item)


def _normalize_document_url(url: str) -> str:
    source_type, token = parse_feishu_url(url)
    parsed = urlsplit(url)
    return urlunsplit(
        (parsed.scheme, parsed.netloc, f"/{source_type.value}/{token}", "", "")
    )
