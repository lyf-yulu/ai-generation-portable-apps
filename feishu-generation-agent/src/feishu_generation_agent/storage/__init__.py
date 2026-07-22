from importlib import import_module
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from feishu_generation_agent.storage.files import FileStore, StoredFile
    from feishu_generation_agent.storage.provider_results import (
        ProviderResultStore,
        StagedProviderResult,
    )
    from feishu_generation_agent.storage.repository import Repository


__all__ = [
    "FileStore",
    "ProviderResultStore",
    "Repository",
    "StagedProviderResult",
    "StoredFile",
]

_EXPORTS = {
    "FileStore": ("feishu_generation_agent.storage.files", "FileStore"),
    "ProviderResultStore": (
        "feishu_generation_agent.storage.provider_results",
        "ProviderResultStore",
    ),
    "Repository": ("feishu_generation_agent.storage.repository", "Repository"),
    "StagedProviderResult": (
        "feishu_generation_agent.storage.provider_results",
        "StagedProviderResult",
    ),
    "StoredFile": ("feishu_generation_agent.storage.files", "StoredFile"),
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
