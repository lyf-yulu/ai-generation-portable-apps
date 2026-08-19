from importlib import import_module
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from feishu_generation_agent.integrations.feishu_client import FeishuClient
    from feishu_generation_agent.integrations.feishu_source import (
        FeishuDocumentSource,
        parse_feishu_url,
    )


__all__ = ["FeishuClient", "FeishuDocumentSource", "parse_feishu_url"]

_EXPORTS = {
    "FeishuClient": (
        "feishu_generation_agent.integrations.feishu_client",
        "FeishuClient",
    ),
    "FeishuDocumentSource": (
        "feishu_generation_agent.integrations.feishu_source",
        "FeishuDocumentSource",
    ),
    "parse_feishu_url": (
        "feishu_generation_agent.integrations.feishu_source",
        "parse_feishu_url",
    ),
}


def __getattr__(name: str) -> Any:
    try:
        module_name, attribute_name = _EXPORTS[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    value = getattr(import_module(module_name), attribute_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted((*globals(), *__all__))
