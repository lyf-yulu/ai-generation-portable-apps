from feishu_generation_agent.domain.plan import ImageReference
from feishu_generation_agent.domain.reference_contract import (
    canonicalize_references,
    reference_tokens,
    remap_prompt_references,
    validate_seedance_prompt,
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


def _video_task(
    prompt: str,
    references: list[ImageReference] | None = None,
) -> dict:
    return {
        "task_id": "task-hotpot",
        "task_type": "image_to_video",
        "prompt": prompt,
        "reference_images": [
            reference.model_dump(mode="json")
            for reference in (
                references
                or [
                    _reference("image-pot", 1),
                    _reference("image-food", 2),
                ]
            )
        ],
    }


def test_seedance_prompt_rejects_hotpot_plan_without_reference_binding() -> None:
    issues = validate_seedance_prompt(
        _video_task(
            "0-3秒：展示空锅。3-8秒：食材入锅。"
            "8-12秒：俯拍成品。"
        ),
        {
            "image-pot": "image/png",
            "image-food": "image/png",
        },
        require_storyboard=True,
    )

    assert any("@图片1" in issue for issue in issues)
    assert any("镜头 1" in issue for issue in issues)
    assert any("绝对秒数" in issue for issue in issues)


def test_seedance_prompt_rejects_tokens_only_listed_before_shots() -> None:
    prompt = (
        "参考 @图片1 中的黄铜毛毡空锅；"
        "参考 @图片2 中的毛毡食材盘。\n"
        "镜头 1：固定镜头展示空锅。\n"
        "镜头 2：近景展示食材入锅。\n"
        "高清，物体稳定不变形，不要生成水印，不要生成 Logo。"
    )

    issues = validate_seedance_prompt(
        _video_task(prompt),
        {
            "image-pot": "image/png",
            "image-food": "image/png",
        },
        require_storyboard=True,
    )

    assert any("镜头 1" in issue and "素材" in issue for issue in issues)
    assert any("镜头 2" in issue and "素材" in issue for issue in issues)
    assert any("@图片1" in issue and "实际镜头" in issue for issue in issues)


def test_seedance_prompt_accepts_understood_references_in_every_shot() -> None:
    prompt = (
        "参考 @图片1 中的黄铜毛毡空锅；"
        "参考 @图片2 中的毛毡食材盘。\n"
        "镜头 1：固定镜头，展示 @图片1 中的黄铜毛毡空锅。\n"
        "镜头 2：近景，@图片2 中的毛毡食材依次落入 "
        "@图片1 中的黄铜毛毡空锅。\n"
        "高清，物体稳定不变形，不要生成水印，不要生成 Logo。"
    )

    assert validate_seedance_prompt(
        _video_task(prompt),
        {
            "image-pot": "image/png",
            "image-food": "image/png",
        },
        require_storyboard=True,
    ) == []


def test_seedance_prompt_requires_semantics_for_every_multimodal_token() -> None:
    references = [
        _reference("image-pot", 1),
        _reference("video-motion", 2, "reference_video"),
        _reference("audio-mood", 3, "reference_audio"),
    ]
    prompt = (
        "@图片1，@视频1，@音频1。"
        "画面稳定，不要生成水印，不要生成 Logo。"
    )

    issues = validate_seedance_prompt(
        _video_task(prompt, references),
        {
            "image-pot": "image/png",
            "video-motion": "video/mp4",
            "audio-mood": "audio/mpeg",
        },
        require_storyboard=False,
    )

    assert any("@图片1" in issue and "具体" in issue for issue in issues)
    assert any("@视频1" in issue and "具体" in issue for issue in issues)
    assert any("@音频1" in issue and "具体" in issue for issue in issues)


def test_seedance_prompt_rejects_noncontinuous_order_and_internal_asset_id() -> None:
    references = [
        _reference("asset-pot", 1),
        _reference("asset-food", 3),
    ]
    issues = validate_seedance_prompt(
        _video_task(
            "参考 @图片1 中的毛毡空锅和 @图片2 中的食材，"
            "保持 asset-pot 的构图。",
            references,
        ),
        {
            "asset-pot": "image/png",
            "asset-food": "image/png",
        },
        require_storyboard=False,
    )

    assert any("1…N" in issue for issue in issues)
    assert any("内部素材 ID" in issue for issue in issues)
