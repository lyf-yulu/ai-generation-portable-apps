import pytest
from pydantic import ValidationError

from feishu_generation_agent.domain.asset_library import (
    AssetKind,
    CharacterAsset,
    normalize_alias,
)


def test_asset_requires_name_and_variant():
    asset = CharacterAsset(
        asset_id="a1",
        name="Sarah",
        variant="晚宴礼服",
        kind=AssetKind.CHARACTER,
        description="女主，金色长发",
        aliases=["莎拉"],
        tags=["女主", "爽剧"],
        model_prefs=["seedream"],
        storage_path="asset-library/a1.png",
        storage_url="https://example.com/asset-library/a1.png",
        mime_type="image/png",
        byte_size=1024,
    )
    assert asset.name == "Sarah"
    assert asset.variant == "晚宴礼服"
    assert asset.match_keys() == ("sarah", "莎拉")


def test_asset_rejects_blank_name():
    with pytest.raises(ValidationError):
        CharacterAsset(
            asset_id="a1",
            name="   ",
            variant="默认",
            kind=AssetKind.CHARACTER,
            storage_path="asset-library/a1.png",
            storage_url="https://example.com/asset-library/a1.png",
            mime_type="image/png",
            byte_size=1,
        )


def test_asset_rejects_non_image_mime():
    with pytest.raises(ValidationError):
        CharacterAsset(
            asset_id="a1",
            name="Sarah",
            variant="默认",
            kind=AssetKind.CHARACTER,
            storage_path="asset-library/a1.txt",
            storage_url="https://example.com/asset-library/a1.txt",
            mime_type="text/plain",
            byte_size=1,
        )


def test_aliases_deduplicate_and_strip():
    asset = CharacterAsset(
        asset_id="a1",
        name="Sarah",
        variant="默认",
        kind=AssetKind.CHARACTER,
        aliases=["  莎拉 ", "莎拉", "Sarah"],
        storage_path="asset-library/a1.png",
        storage_url="https://example.com/asset-library/a1.png",
        mime_type="image/png",
        byte_size=1,
    )
    assert asset.aliases == ["莎拉"]


def test_normalize_alias_lowercases_ascii_only():
    assert normalize_alias("  Sarah ") == "sarah"
    assert normalize_alias("莎拉") == "莎拉"
