from datetime import datetime, timezone

from feishu_generation_agent.domain.asset_library import AssetKind, CharacterAsset
from feishu_generation_agent.domain.character_matcher import (
    CharacterMatch,
    match_characters,
)
from feishu_generation_agent.domain.document import DocumentBlock


def _asset(
    asset_id: str,
    name: str,
    variant: str = "默认",
    aliases: list[str] | None = None,
) -> CharacterAsset:
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    return CharacterAsset(
        asset_id=asset_id,
        name=name,
        variant=variant,
        kind=AssetKind.CHARACTER,
        aliases=aliases or [],
        storage_path=f"asset-library/{asset_id}.png",
        storage_url=f"https://media.example.com/asset-library/{asset_id}.png",
        mime_type="image/png",
        byte_size=10,
        created_at=now,
        updated_at=now,
    )


def _block(block_id: str, text: str, order: int = 0) -> DocumentBlock:
    return DocumentBlock(
        block_id=block_id,
        parent_id=None,
        block_type="text",
        order=order,
        path=[block_id],
        text=text,
    )


def test_matches_ascii_name_case_insensitively():
    blocks = [_block("b1", "画面描述：Victor 中景，脸部因愤怒而扭曲")]

    matches = match_characters(blocks, [_asset("a1", "Victor")])

    assert matches == [
        CharacterMatch(
            asset_id="a1",
            name="Victor",
            variant="默认",
            matched_key="victor",
            block_ids=("b1",),
        )
    ]


def test_matches_chinese_alias():
    blocks = [_block("b1", "莎拉与索菲亚握手言和")]

    matches = match_characters(
        blocks, [_asset("a1", "Sarah", aliases=["莎拉"])]
    )

    assert [item.matched_key for item in matches] == ["莎拉"]


def test_collects_every_block_that_mentions_the_asset():
    blocks = [
        _block("b1", "Sarah 站在门口", 0),
        _block("b2", "无关内容", 1),
        _block("b3", "镜头切到 Sarah 的背影", 2),
    ]

    matches = match_characters(blocks, [_asset("a1", "Sarah")])

    assert matches[0].block_ids == ("b1", "b3")


def test_unmentioned_assets_are_not_returned():
    blocks = [_block("b1", "只有 Victor 出场")]

    matches = match_characters(
        blocks, [_asset("a1", "Victor"), _asset("a2", "Sophia")]
    )

    assert [item.name for item in matches] == ["Victor"]


def test_same_name_multiple_variants_all_match():
    blocks = [_block("b1", "Sarah 换上战斗装")]

    matches = match_characters(
        blocks,
        [
            _asset("a1", "Sarah", "晚宴礼服"),
            _asset("a2", "Sarah", "战斗装"),
        ],
    )

    assert {item.variant for item in matches} == {"晚宴礼服", "战斗装"}


def test_ascii_name_requires_word_boundary():
    """Sara 不应命中 Sarah，避免短名误匹配长名。"""
    blocks = [_block("b1", "Sarah 出场")]

    matches = match_characters(blocks, [_asset("a1", "Sara")])

    assert matches == []


def test_chinese_name_matches_without_word_boundary():
    """中文没有词边界，子串命中即算。"""
    blocks = [_block("b1", "画面中索菲亚微笑")]

    matches = match_characters(blocks, [_asset("a1", "索菲亚")])

    assert len(matches) == 1


def test_blocks_without_text_are_skipped():
    blocks = [_block("b1", ""), _block("b2", "Victor 登场", 1)]

    matches = match_characters(blocks, [_asset("a1", "Victor")])

    assert matches[0].block_ids == ("b2",)


def test_results_are_deterministically_ordered():
    blocks = [_block("b1", "Sarah、Victor 与索菲亚同时出现")]
    assets = [
        _asset("a3", "索菲亚"),
        _asset("a1", "Victor"),
        _asset("a2", "Sarah"),
    ]

    first = match_characters(blocks, assets)
    second = match_characters(blocks, list(reversed(assets)))

    assert [item.name for item in first] == [item.name for item in second]


def test_empty_inputs_yield_no_matches():
    assert match_characters([], [_asset("a1", "Victor")]) == []
    assert match_characters([_block("b1", "Victor")], []) == []
