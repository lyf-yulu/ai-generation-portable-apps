from feishu_generation_agent.domain.plan import ImageReference
from feishu_generation_agent.domain.reference_contract import (
    canonicalize_references,
    reference_tokens,
    remap_prompt_references,
)


def _reference(
    asset_id: str,
    order: int,
    role: str = "reference_image",
) -> ImageReference:
    return ImageReference(asset_id=asset_id, role=role, order=order)


def test_canonicalize_references_closes_middle_gap() -> None:
    result = canonicalize_references(
        [
            _reference("asset-a", 1),
            _reference("asset-c", 3),
        ]
    )

    assert [
        (reference.asset_id, reference.order) for reference in result
    ] == [("asset-a", 1), ("asset-c", 2)]


def test_canonicalize_references_rejects_ambiguous_duplicate_order() -> None:
    try:
        canonicalize_references(
            [
                _reference("asset-a", 1),
                _reference("asset-b", 1),
            ]
        )
    except ValueError as exc:
        assert str(exc) == "reference orders must be unique"
    else:
        raise AssertionError("duplicate reference order must be rejected")


def test_reference_tokens_number_each_media_type_separately() -> None:
    references = [
        _reference("image-a", 1),
        _reference("video-a", 2, "reference_video"),
        _reference("image-b", 3),
        _reference("audio-a", 4, "reference_audio"),
    ]
    mime_types = {
        "image-a": "image/png",
        "video-a": "video/mp4",
        "image-b": "image/jpeg",
        "audio-a": "audio/mpeg",
    }

    assert reference_tokens(references, mime_types) == {
        "image-a": "@图片1",
        "video-a": "@视频1",
        "image-b": "@图片2",
        "audio-a": "@音频1",
    }


def test_remap_prompt_references_preserves_surviving_asset_identity() -> None:
    old = [
        _reference("asset-a", 1),
        _reference("asset-b", 2),
        _reference("asset-c", 3),
    ]
    new = [
        _reference("asset-a", 1),
        _reference("asset-c", 2),
    ]
    mime_types = {
        "asset-a": "image/png",
        "asset-b": "image/png",
        "asset-c": "image/png",
    }

    result = remap_prompt_references(
        "@图片1 中的锅；@图片2 中的碗；@图片3 中的桌面",
        old,
        new,
        mime_types,
    )

    assert result == "@图片1 中的锅；碗；@图片2 中的桌面"


def test_remap_prompt_references_avoids_cascading_number_replacement() -> None:
    old = [
        _reference("asset-a", 1),
        _reference("asset-b", 2),
        _reference("asset-c", 3),
        _reference("asset-d", 4),
    ]
    new = [
        _reference("asset-a", 1),
        _reference("asset-c", 2),
        _reference("asset-d", 3),
    ]
    mime_types = {reference.asset_id: "image/png" for reference in old}

    result = remap_prompt_references(
        "参考图3的桌面延续到第4张参考图中的成品",
        old,
        new,
        mime_types,
    )

    assert result == "参考图2的桌面延续到第3张参考图中的成品"
