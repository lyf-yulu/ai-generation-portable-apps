from datetime import datetime, timezone

from feishu_generation_agent.domain.asset_library import AssetKind, CharacterAsset
from feishu_generation_agent.web.schemas import (
    AssetLibraryItem,
    AssetLibraryListResponse,
    AssetLibraryUpdateRequest,
)


def _asset() -> CharacterAsset:
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    return CharacterAsset(
        asset_id="a1",
        name="Sarah",
        variant="晚宴礼服",
        kind=AssetKind.CHARACTER,
        description="女主",
        aliases=["莎拉"],
        tags=["女主"],
        model_prefs=["seedream"],
        prompt_fragment="金色长发",
        storage_path="asset-library/a1.png",
        storage_url="https://media.example.com/asset-library/a1.png",
        mime_type="image/png",
        byte_size=10,
        created_at=now,
        updated_at=now,
    )


def test_item_from_domain_exposes_url_not_path():
    item = AssetLibraryItem.from_domain(_asset())
    assert item.asset_id == "a1"
    assert item.name == "Sarah"
    assert item.variant == "晚宴礼服"
    assert item.url == "https://media.example.com/asset-library/a1.png"
    assert not hasattr(item, "storage_path")


def test_list_response_wraps_items():
    response = AssetLibraryListResponse.from_domain([_asset()])
    assert response.total == 1
    assert response.items[0].name == "Sarah"


def test_update_request_all_fields_optional():
    request = AssetLibraryUpdateRequest()
    assert request.name is None
    assert request.tags is None
