from collections.abc import Mapping
import re
from typing import Any, Callable

from feishu_generation_agent.domain.plan import ImageReference


_MEDIA_LABELS = {
    "image": ("图片", "参考图", "张参考图"),
    "video": ("视频", "参考视频", "个参考视频"),
    "audio": ("音频", "参考音频", "个参考音频"),
}
_SHOT_MARKER = re.compile(r"镜头\s*(\d+)\s*[：:]")
_ABSOLUTE_SECONDS = re.compile(
    r"\d+(?:\.\d+)?\s*[-–—~～至到]\s*\d+(?:\.\d+)?\s*秒"
)


class ReferenceRemapError(ValueError):
    pass


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
    *,
    replacement_asset_ids: Mapping[str, str] | None = None,
) -> str:
    old_positions = {
        asset_id: (media_type, media_index)
        for asset_id, media_type, media_index in _old_reference_positions(
            old_references,
            mime_types,
            prompt,
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
    replacements = replacement_asset_ids or {}
    for asset_offset, (asset_id, position) in enumerate(old_positions.items()):
        media_type, old_index = position
        for style_offset, (pattern, renderer) in enumerate(
            _reference_patterns(media_type, old_index)
        ):
            placeholder = f"\ufff0REF{asset_offset}_{style_offset}\ufff1"
            rewritten = pattern.sub(placeholder, rewritten)
            replacement = None
            target_asset_id = replacements.get(asset_id, asset_id)
            if target_asset_id in new_positions:
                new_media_type, new_index = new_positions[target_asset_id]
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


def validate_seedance_prompt(
    task: Mapping[str, Any],
    mime_types: Mapping[str, str],
    *,
    require_storyboard: bool,
) -> list[str]:
    prompt = task.get("prompt")
    if not isinstance(prompt, str):
        return ["Seedance prompt 必须是字符串"]
    raw_references = task.get("reference_images")
    if not isinstance(raw_references, list):
        return ["Seedance reference_images 必须是列表"]
    try:
        references = [
            ImageReference.model_validate(reference)
            for reference in raw_references
        ]
    except Exception:
        return ["Seedance reference_images 无法解析"]

    issues: list[str] = []
    ordered = sorted(references, key=lambda item: item.order)
    if [reference.order for reference in ordered] != list(
        range(1, len(ordered) + 1)
    ):
        issues.append("Seedance 参考素材 order 必须按 1…N 连续排列")
    try:
        tokens = reference_tokens(ordered, mime_types)
    except ValueError as exc:
        issues.append(str(exc))
        return issues

    for asset_id, token in tokens.items():
        if re.search(
            rf"(?<![\w-]){re.escape(asset_id)}(?![\w-])",
            prompt,
        ):
            issues.append(
                f"Seedance prompt 不得包含内部素材 ID {asset_id}"
            )
        if token not in prompt:
            issues.append(
                f"Seedance prompt 缺少素材引用 {token}"
            )
            continue
        semantic_pattern = re.compile(
            rf"{re.escape(token)}\s*"
            r"(?:中\s*的|中的|的|[（(])\s*"
            r"[^，。；;\n]{2,}"
        )
        if semantic_pattern.search(prompt) is None:
            issues.append(
                f"Seedance prompt 中 {token} 必须绑定具体主体、场景、动作、"
                "运镜或声音"
            )

    if _ABSOLUTE_SECONDS.search(prompt):
        issues.append(
            "Seedance 多分镜 prompt 禁止绝对秒数，必须使用镜头 1/2/3 顺序"
        )

    if not require_storyboard:
        return issues

    matches = list(_SHOT_MARKER.finditer(prompt))
    if len(matches) < 2:
        issues.append(
            "Seedance 多分镜 prompt 必须包含镜头 1、镜头 2 等顺序分镜"
        )
        return issues
    shot_numbers = [int(match.group(1)) for match in matches]
    if shot_numbers != list(range(1, len(shot_numbers) + 1)):
        issues.append("Seedance 镜头编号必须唯一并按 1…N 连续排列")

    shot_segments: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        end = (
            matches[index + 1].start()
            if index + 1 < len(matches)
            else len(prompt)
        )
        shot_segments.append((match.group(1), prompt[match.start():end]))

    token_values = set(tokens.values())
    tokens_used_in_shots: set[str] = set()
    for shot_number, segment in shot_segments:
        used = {token for token in token_values if token in segment}
        if not used:
            issues.append(
                f"镜头 {shot_number} 缺少明确的 Seedance 参考素材绑定"
            )
        tokens_used_in_shots.update(used)
    for token in sorted(token_values - tokens_used_in_shots):
        issues.append(
            f"{token} 只被罗列但没有用于任何实际镜头"
        )

    if not any(
        keyword in prompt for keyword in ("稳定", "不变形", "连贯")
    ):
        issues.append("Seedance 多分镜 prompt 缺少画面稳定性约束")
    if "水印" not in prompt:
        issues.append("Seedance 多分镜 prompt 缺少无水印约束")
    if "logo" not in prompt.lower():
        issues.append("Seedance 多分镜 prompt 缺少无 Logo 约束")
    return issues


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


def _old_reference_positions(
    references: list[ImageReference],
    mime_types: Mapping[str, str],
    prompt: str,
) -> list[tuple[str, str, int]]:
    sequential = _reference_positions(references, mime_types)
    orders = sorted(reference.order for reference in references)
    if orders == list(range(1, len(orders) + 1)):
        return sequential
    if len({media_type for _, media_type, _ in sequential}) > 1:
        raise ReferenceRemapError(
            "旧任务的混合媒体参考编号存在断档，无法安全判断素材身份；"
            "请重新规划任务或重新添加参考素材"
        )

    by_asset_id = {reference.asset_id: reference for reference in references}
    positions: list[tuple[str, str, int]] = []
    for asset_id, media_type, sequential_index in sequential:
        visible_index = by_asset_id[asset_id].order
        if visible_index == sequential_index:
            positions.append((asset_id, media_type, sequential_index))
            continue
        sequential_used = _reference_mentioned(
            prompt,
            media_type,
            sequential_index,
        )
        visible_used = _reference_mentioned(
            prompt,
            media_type,
            visible_index,
        )
        if sequential_used and visible_used:
            raise ReferenceRemapError(
                "ambiguous legacy reference numbering; "
                f"both {_MEDIA_LABELS[media_type][0]}{sequential_index} and "
                f"{_MEDIA_LABELS[media_type][0]}{visible_index} are present"
            )
        positions.append(
            (
                asset_id,
                media_type,
                visible_index if visible_used else sequential_index,
            )
        )
    return positions


def _reference_mentioned(
    prompt: str,
    media_type: str,
    index: int,
) -> bool:
    return any(
        pattern.search(prompt) is not None
        for pattern, _ in _reference_patterns(media_type, index)
    )


def _media_type(mime_type: str) -> str:
    for candidate in ("image", "video", "audio"):
        if mime_type.startswith(f"{candidate}/"):
            return candidate
    raise ValueError(f"unsupported reference MIME type: {mime_type}")


def _reference_patterns(
    media_type: str,
    index: int,
) -> list[tuple[re.Pattern[str], Callable[[str, int], str]]]:
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
