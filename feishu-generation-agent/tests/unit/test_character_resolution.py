from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from feishu_generation_agent.domain.asset_library import AssetKind, CharacterAsset
from feishu_generation_agent.domain.character_matcher import CharacterMatch
from feishu_generation_agent.domain.document import (
    DocumentBlock,
    NormalizedDocument,
    SourceType,
)
from feishu_generation_agent.graph.nodes import (
    _character_context_argument,
    _resolve_character_assets,
)
from feishu_generation_agent.integrations.character_semantic_matcher import (
    SemanticMatch,
    SemanticMatchResult,
    UnresolvedCandidate,
)


def _asset(asset_id: str, name: str, variant: str = "默认", **updates: Any):
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    payload: dict[str, Any] = {
        "asset_id": asset_id,
        "name": name,
        "variant": variant,
        "kind": AssetKind.CHARACTER,
        "storage_path": f"asset-library/{asset_id}.png",
        "storage_url": f"https://media.example.com/asset-library/{asset_id}.png",
        "mime_type": "image/png",
        "byte_size": 10,
        "created_at": now,
        "updated_at": now,
    }
    payload.update(updates)
    return CharacterAsset(**payload)


def _document(text: str = "Victor 中景，脸部因愤怒扭曲") -> NormalizedDocument:
    return NormalizedDocument(
        document_id="doc-1",
        title="CG 需求",
        revision=1,
        source_type=SourceType.WIKI,
        source_token="token-1",
        blocks=[
            DocumentBlock(
                block_id="b1",
                parent_id=None,
                block_type="text",
                order=0,
                path=["b1"],
                text=text,
            )
        ],
        text_view=f"[block:b1] {text}",
        media_assets=[],
    )


class _Store:
    def __init__(self, assets: list[CharacterAsset]) -> None:
        self.assets = assets
        self.calls = 0

    async def list_all(self, **_kwargs: Any) -> list[CharacterAsset]:
        self.calls += 1
        return self.assets


class _Matcher:
    def __init__(self, result: SemanticMatchResult | Exception) -> None:
        self.result = result
        self.calls = 0
        self.anchors_seen: list[list[CharacterMatch]] = []

    async def match(self, text, library, *, anchors) -> SemanticMatchResult:
        del text, library
        self.calls += 1
        self.anchors_seen.append(list(anchors))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _services(store: Any = None, matcher: Any = None) -> Any:
    return SimpleNamespace(
        asset_library_store=store,
        character_matcher=matcher,
    )


async def test_exact_match_resolves_without_calling_model():
    store = _Store([_asset("a1", "Victor", prompt_fragment="络腮胡中年男性")])
    matcher = _Matcher(SemanticMatchResult())

    resolved = await _resolve_character_assets(
        _document(), _services(store, matcher)
    )

    assert [item.name for item in resolved] == ["Victor"]
    assert resolved[0].prompt_fragment == "络腮胡中年男性"


async def test_semantic_matches_are_merged_with_exact_matches():
    store = _Store([_asset("a1", "Victor"), _asset("a2", "Sarah", "晚宴礼服")])
    matcher = _Matcher(
        SemanticMatchResult(
            matches=(
                SemanticMatch(
                    asset_id="a2",
                    block_ids=("b1",),
                    confidence=0.9,
                    reason="金发礼服",
                ),
            )
        )
    )

    resolved = await _resolve_character_assets(
        _document(), _services(store, matcher)
    )

    assert {item.name for item in resolved} == {"Victor", "Sarah"}


async def test_exact_matches_are_passed_as_anchors():
    store = _Store([_asset("a1", "Victor")])
    matcher = _Matcher(SemanticMatchResult())

    await _resolve_character_assets(_document(), _services(store, matcher))

    assert [anchor.name for anchor in matcher.anchors_seen[0]] == ["Victor"]


async def test_semantic_failure_falls_back_to_exact_matches():
    """语义匹配挂掉不能拖垮整个 run，退回精确匹配结果。"""
    store = _Store([_asset("a1", "Victor")])
    matcher = _Matcher(RuntimeError("model down"))

    resolved = await _resolve_character_assets(
        _document(), _services(store, matcher)
    )

    assert [item.name for item in resolved] == ["Victor"]


async def test_no_store_yields_no_resolution():
    resolved = await _resolve_character_assets(_document(), _services())

    assert resolved == []


async def test_no_matcher_still_returns_exact_matches():
    store = _Store([_asset("a1", "Victor")])

    resolved = await _resolve_character_assets(
        _document(), _services(store, None)
    )

    assert [item.name for item in resolved] == ["Victor"]


async def test_store_failure_is_swallowed():
    class BrokenStore:
        async def list_all(self, **_kwargs: Any):
            raise RuntimeError("sqlite locked")

    resolved = await _resolve_character_assets(
        _document(), _services(BrokenStore(), _Matcher(SemanticMatchResult()))
    )

    assert resolved == []


async def test_unmentioned_characters_are_not_resolved():
    store = _Store([_asset("a1", "Sophia")])
    matcher = _Matcher(SemanticMatchResult())

    resolved = await _resolve_character_assets(
        _document(), _services(store, matcher)
    )

    assert resolved == []


def test_character_context_argument_is_omitted_when_empty():
    class Planner:
        async def plan(self, document, visions, feedback=None, character_context=None):
            raise NotImplementedError

    assert _character_context_argument(Planner(), []) == {}


def test_character_context_argument_describes_resolved_assets():
    class Planner:
        async def plan(self, document, visions, feedback=None, character_context=None):
            raise NotImplementedError

    resolved = [
        _asset("a1", "Sarah", "晚宴礼服", prompt_fragment="金色长发"),
    ]

    argument = _character_context_argument(Planner(), resolved)

    context = argument["character_context"]
    assert "Sarah" in context
    assert "晚宴礼服" in context
    assert "金色长发" in context
    assert "a1" in context


def test_character_context_argument_is_omitted_for_legacy_planner():
    """老 planner 没有该参数，必须省略而不是 TypeError。"""

    class LegacyPlanner:
        async def plan(self, document, visions, feedback=None):
            raise NotImplementedError

    resolved = [_asset("a1", "Sarah")]

    assert _character_context_argument(LegacyPlanner(), resolved) == {}


async def test_auto_ingested_characters_are_resolved_too(tmp_path):
    """未知角色自动建档后必须一并挂上，否则本次出图仍然缺参考图。"""
    from feishu_generation_agent.domain.document import MediaAsset
    from feishu_generation_agent.integrations.character_semantic_matcher import (
        UnresolvedCandidate,
    )

    image = tmp_path / "mike.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    document = NormalizedDocument(
        document_id="doc-1",
        title="CG 需求",
        revision=1,
        source_type=SourceType.WIKI,
        source_token="token-1",
        blocks=[
            DocumentBlock(
                block_id="b1",
                parent_id=None,
                block_type="text",
                order=0,
                path=["b1"],
                text="新角色 Mike 登场",
            ),
            DocumentBlock(
                block_id="b2",
                parent_id=None,
                block_type="image",
                order=1,
                path=["b2"],
                text="",
                image_asset_id="image-1",
            ),
        ],
        text_view="[block:b1] 新角色 Mike 登场",
        media_assets=[
            MediaAsset(
                asset_id="image-1",
                source_block_id="b2",
                origin="feishu",
                local_path=image,
                mime_type="image/png",
                size=8,
                sha256="a" * 64,
            )
        ],
    )

    created_asset = _asset("new-1", "Mike")

    class Store:
        def __init__(self) -> None:
            self.assets = [_asset("a1", "Victor")]

        async def list_all(self, **_kwargs: Any):
            return self.assets

        async def create(self, **_kwargs: Any):
            return created_asset

    matcher = _Matcher(
        SemanticMatchResult(
            unresolved_candidates=(
                UnresolvedCandidate(
                    proposed_name="Mike", block_ids=("b1",), reason="库里没有"
                ),
            )
        )
    )

    resolved = await _resolve_character_assets(
        document, _services(Store(), matcher)
    )

    assert "Mike" in [item.name for item in resolved]
