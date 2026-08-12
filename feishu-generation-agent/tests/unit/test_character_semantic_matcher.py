import copy
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from feishu_generation_agent.domain.asset_library import AssetKind, CharacterAsset
from feishu_generation_agent.domain.character_matcher import CharacterMatch
from feishu_generation_agent.domain.errors import AgentError
from feishu_generation_agent.integrations.character_semantic_matcher import (
    DeepSeekCharacterMatcher,
)


class FakeMatchModel:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = responses
        self.requests: list[list[dict[str, Any]]] = []

    def bind(self, **_kwargs: Any) -> "FakeMatchModel":
        return self

    async def ainvoke(
        self,
        messages: list[dict[str, Any]],
        config: dict[str, Any] | None = None,
    ) -> object:
        self.requests.append(copy.deepcopy(messages))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return SimpleNamespace(content=response, additional_kwargs={})


def _asset(
    asset_id: str,
    name: str,
    variant: str = "默认",
    description: str = "",
) -> CharacterAsset:
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    return CharacterAsset(
        asset_id=asset_id,
        name=name,
        variant=variant,
        kind=AssetKind.CHARACTER,
        description=description,
        storage_path=f"asset-library/{asset_id}.png",
        storage_url=f"https://media.example.com/asset-library/{asset_id}.png",
        mime_type="image/png",
        byte_size=10,
        created_at=now,
        updated_at=now,
    )


def _payload(**updates: object) -> str:
    body: dict[str, Any] = {"matches": [], "unresolved_candidates": []}
    body.update(updates)
    return json.dumps(body, ensure_ascii=False)


TEXT = "[block:b1] 两位女性握手言和，金发那位穿着晚宴礼服"


async def test_returns_semantic_matches():
    model = FakeMatchModel(
        [
            _payload(
                matches=[
                    {
                        "asset_id": "a1",
                        "block_ids": ["b1"],
                        "confidence": 0.9,
                        "reason": "金发晚宴礼服对应 Sarah 晚宴礼服",
                    }
                ]
            )
        ]
    )
    matcher = DeepSeekCharacterMatcher(model)

    result = await matcher.match(
        TEXT, [_asset("a1", "Sarah", "晚宴礼服")], anchors=[]
    )

    assert [item.asset_id for item in result.matches] == ["a1"]
    assert result.matches[0].block_ids == ("b1",)
    assert result.unresolved_candidates == ()


async def test_returns_unresolved_candidates():
    model = FakeMatchModel(
        [
            _payload(
                unresolved_candidates=[
                    {
                        "proposed_name": "Mike",
                        "block_ids": ["b1"],
                        "reason": "文档提到 Mike 但素材库没有",
                    }
                ]
            )
        ]
    )
    matcher = DeepSeekCharacterMatcher(model)

    result = await matcher.match(TEXT, [], anchors=[])

    assert [item.proposed_name for item in result.unresolved_candidates] == [
        "Mike"
    ]
    assert result.unresolved_candidates[0].block_ids == ("b1",)


async def test_anchors_are_sent_so_model_does_not_redo_exact_matches():
    model = FakeMatchModel([_payload()])
    matcher = DeepSeekCharacterMatcher(model)
    anchors = [
        CharacterMatch(
            asset_id="a1",
            name="Victor",
            variant="默认",
            matched_key="victor",
            block_ids=("b1",),
        )
    ]

    await matcher.match(TEXT, [_asset("a1", "Victor")], anchors=anchors)

    user_prompt = model.requests[0][1]["content"]
    assert "Victor" in user_prompt
    assert "已匹配" in user_prompt


async def test_unknown_asset_id_from_model_is_dropped():
    """模型幻觉出不存在的 asset_id 时必须丢弃，不能污染计划。"""
    model = FakeMatchModel(
        [
            _payload(
                matches=[
                    {"asset_id": "ghost", "block_ids": ["b1"], "confidence": 1.0}
                ]
            )
        ]
    )
    matcher = DeepSeekCharacterMatcher(model)

    result = await matcher.match(TEXT, [_asset("a1", "Sarah")], anchors=[])

    assert result.matches == ()


async def test_low_confidence_matches_are_dropped():
    model = FakeMatchModel(
        [
            _payload(
                matches=[
                    {"asset_id": "a1", "block_ids": ["b1"], "confidence": 0.1}
                ]
            )
        ]
    )
    matcher = DeepSeekCharacterMatcher(model, min_confidence=0.5)

    result = await matcher.match(TEXT, [_asset("a1", "Sarah")], anchors=[])

    assert result.matches == ()


async def test_empty_document_skips_model_call():
    """没有文档内容时不必调模型，省一次 API。

    注意不能按「素材库为空」短路：文档里的未知角色仍要被识别成
    unresolved_candidates，供后续自动入库。
    """
    model = FakeMatchModel([])
    matcher = DeepSeekCharacterMatcher(model)

    result = await matcher.match("   ", [], anchors=[])

    assert result.matches == ()
    assert result.unresolved_candidates == ()
    assert model.requests == []


async def test_empty_library_still_detects_unknown_characters():
    model = FakeMatchModel(
        [
            _payload(
                unresolved_candidates=[
                    {"proposed_name": "Mike", "block_ids": ["b1"]}
                ]
            )
        ]
    )
    matcher = DeepSeekCharacterMatcher(model)

    result = await matcher.match(TEXT, [], anchors=[])

    assert [item.proposed_name for item in result.unresolved_candidates] == [
        "Mike"
    ]


async def test_malformed_json_raises_agent_error():
    model = FakeMatchModel(["not json at all"])
    matcher = DeepSeekCharacterMatcher(model)

    with pytest.raises(AgentError):
        await matcher.match(TEXT, [_asset("a1", "Sarah")], anchors=[])


async def test_model_failure_raises_agent_error():
    model = FakeMatchModel([RuntimeError("upstream down")])
    matcher = DeepSeekCharacterMatcher(model)

    with pytest.raises(AgentError):
        await matcher.match(TEXT, [_asset("a1", "Sarah")], anchors=[])


async def test_library_entries_appear_in_prompt():
    model = FakeMatchModel([_payload()])
    matcher = DeepSeekCharacterMatcher(model)

    await matcher.match(
        TEXT,
        [_asset("a1", "Sarah", "晚宴礼服", description="金色长发")],
        anchors=[],
    )

    user_prompt = model.requests[0][1]["content"]
    assert "Sarah" in user_prompt
    assert "晚宴礼服" in user_prompt
    assert "金色长发" in user_prompt
