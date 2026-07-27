from collections.abc import Mapping
import re

from feishu_generation_agent.domain.plan import ImageReference


_MEDIA_LABELS = {
    "image": ("图片", "参考图", "张参考图"),
    "video": ("视频", "参考视频", "个参考视频"),
    "audio": ("音频", "参考音频", "个参考音频"),
}


def canonicalize_references(
    references: list[ImageReference],
) -> list[ImageReference]:
    ordered = sorted(references, key=lambda item: item.order)
    if len({item.order for item in ordered}) != len(ordered):
        raise ValueError("reference orders must be unique")
    return [
        reference.model_copy(update={"order": index})
        for index, reference in enumerate(ordered, start=1)
    ]


def reference_tokens(
    references: list[ImageReference],
    mime_types: Mapping[str, str],
) -> dict[str, str]:
    return {
        asset_id: f"@{_MEDIA_LABELS[media_type][0]}{media_index}"
        for asset_id, media_type, media_index in _reference_positions(
            references, mime_types
        )
    }


def remap_prompt_references(
    prompt: str,
    old_references: list[ImageReference],
    new_references: list[ImageReference],
    mime_types: Mapping[str, str],
) -> str:
    old_positions = {
        asset_id: (media_type, media_index)
        for asset_id, media_type, media_index in _reference_positions(
            old_references, mime_types
        )
    }
    new_positions = {
        asset_id: (media_type, media_index)
        for asset_id, media_type, media_index in _reference_positions(
            new_references, mime_types
        )
    }
    rewritten = prompt
    placeholders: list[tuple[str, str | None]] = []
    for asset_offset, (asset_id, position) in enumerate(old_positions.items()):
        media_type, old_index = position
        for style_offset, (pattern, renderer) in enumerate(
            _reference_patterns(media_type, old_index)
        ):
            placeholder = f"\ufff0REF{asset_offset}_{style_offset}\ufff1"
            rewritten = pattern.sub(placeholder, rewritten)
            replacement = None
            if asset_id in new_positions:
                new_media_type, new_index = new_positions[asset_id]
                replacement = renderer(new_media_type, new_index)
            placeholders.append((placeholder, replacement))

    for placeholder, replacement in placeholders:
        if replacement is not None:
            rewritten = rewritten.replace(placeholder, replacement)
            continue
        rewritten = re.sub(
            rf"(?:参考\s*)?{re.escape(placeholder)}\s*(?:中\s*的|中的|的)",
            "",
            rewritten,
        )
        rewritten = rewritten.replace(placeholder, "")
    return rewritten


def _reference_positions(
    references: list[ImageReference],
    mime_types: Mapping[str, str],
) -> list[tuple[str, str, int]]:
    counts = {"image": 0, "video": 0, "audio": 0}
    positions: list[tuple[str, str, int]] = []
    for reference in sorted(references, key=lambda item: item.order):
        media_type = _media_type(mime_types.get(reference.asset_id, ""))
        counts[media_type] += 1
        positions.append(
            (reference.asset_id, media_type, counts[media_type])
        )
    return positions


def _media_type(mime_type: str) -> str:
    for candidate in ("image", "video", "audio"):
        if mime_type.startswith(f"{candidate}/"):
            return candidate
    raise ValueError(f"unsupported reference MIME type: {mime_type}")


def _reference_patterns(
    media_type: str,
    index: int,
) -> list[tuple[re.Pattern[str], object]]:
    plain, reference, ordinal = _MEDIA_LABELS[media_type]
    boundary = r"(?!\d)"
    return [
        (
            re.compile(rf"@{plain}{index}{boundary}"),
            lambda kind, value: f"@{_MEDIA_LABELS[kind][0]}{value}",
        ),
        (
            re.compile(rf"第\s*{index}\s*{ordinal}{boundary}"),
            lambda kind, value: (
                f"第{value}{_MEDIA_LABELS[kind][2]}"
            ),
        ),
        (
            re.compile(rf"{reference}\s*{index}{boundary}"),
            lambda kind, value: (
                f"{_MEDIA_LABELS[kind][1]}{value}"
            ),
        ),
        (
            re.compile(rf"(?<![@\u4e00-\u9fff]){plain}\s*{index}{boundary}"),
            lambda kind, value: f"{_MEDIA_LABELS[kind][0]}{value}",
        ),
    ]
