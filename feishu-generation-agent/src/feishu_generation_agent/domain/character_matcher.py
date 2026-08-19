"""把文档里出现的角色名匹配到素材库条目。

这是「精确匹配」层：按素材的 name 与 aliases 做字面命中，不调模型。
命中不了的候选交给语义匹配层（integrations/character_semantic_matcher.py）。

匹配规则按语言分开处理：
- ASCII 名要求词边界，否则 "Sara" 会命中 "Sarah"，把两个角色搞混。
- 中文没有词边界概念，子串命中即算（"索菲亚" 出现在 "画面中索菲亚微笑" 里）。
"""

from dataclasses import dataclass
import re

from feishu_generation_agent.domain.asset_library import (
    CharacterAsset,
    normalize_alias,
)
from feishu_generation_agent.domain.document import DocumentBlock


@dataclass(frozen=True, slots=True)
class CharacterMatch:
    asset_id: str
    name: str
    variant: str
    matched_key: str
    block_ids: tuple[str, ...]


def match_characters(
    blocks: list[DocumentBlock],
    assets: list[CharacterAsset],
) -> list[CharacterMatch]:
    """返回文档里被提到的素材，按素材 asset_id 稳定排序。"""
    if not blocks or not assets:
        return []

    texts = [
        (block.block_id, block.text)
        for block in sorted(blocks, key=lambda item: item.order)
        if block.text and block.text.strip()
    ]
    if not texts:
        return []

    matches: list[CharacterMatch] = []
    for asset in sorted(assets, key=lambda item: item.asset_id):
        hit_key: str | None = None
        hit_blocks: list[str] = []
        for key in asset.match_keys():
            block_ids = [
                block_id
                for block_id, text in texts
                if _mentions(text, key)
            ]
            if block_ids:
                hit_key = key
                hit_blocks = block_ids
                break
        if hit_key is None:
            continue
        matches.append(
            CharacterMatch(
                asset_id=asset.asset_id,
                name=asset.name,
                variant=asset.variant,
                matched_key=hit_key,
                block_ids=tuple(hit_blocks),
            )
        )
    return matches


def _mentions(text: str, key: str) -> bool:
    if not key:
        return False
    if key.isascii():
        pattern = rf"(?<![0-9A-Za-z_]){re.escape(key)}(?![0-9A-Za-z_])"
        return re.search(pattern, text, re.IGNORECASE) is not None
    return key in normalize_alias(text) or key in text
