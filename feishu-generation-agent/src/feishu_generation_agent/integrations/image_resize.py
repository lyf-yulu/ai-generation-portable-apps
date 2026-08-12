"""把一张生成图渲染成需求要求的多个尺寸变体。

需求文档常见形态：同一个画面概念要交付多个尺寸（例：1080x2080 安全区版
与 1700x2500 完整版）。计划阶段一个概念只产出一个 entry，出图后由本模块
按 size_variants 渲染出各尺寸，作为独立产物一并交付。

缩放策略：等比缩放到能覆盖目标框，再居中裁切（cover + center crop）。
需求明确写了「禁止拉伸图片」，所以不用直接 stretch。
"""

from dataclasses import dataclass
from pathlib import Path

from PIL import Image


_RESAMPLE = Image.Resampling.LANCZOS


@dataclass(frozen=True, slots=True)
class SizeVariant:
    width: int
    height: int

    def __str__(self) -> str:
        return f"{self.width}x{self.height}"


@dataclass(frozen=True, slots=True)
class RenderedVariant:
    variant: str
    path: Path
    width: int
    height: int


def parse_size_variant(value: str) -> SizeVariant:
    normalized = value.strip().lower().replace("×", "x").replace("*", "x")
    width_text, separator, height_text = normalized.partition("x")
    if (
        not separator
        or not width_text.isdigit()
        or not height_text.isdigit()
    ):
        raise ValueError(f"尺寸变体需要形如 1700x2500，收到 {value!r}")
    width = int(width_text)
    height = int(height_text)
    if width <= 0 or height <= 0:
        raise ValueError(f"尺寸变体必须为正数，收到 {value!r}")
    return SizeVariant(width, height)


def render_size_variants(
    source: Path,
    variants: list[str],
    *,
    output_dir: Path,
) -> list[RenderedVariant]:
    """按 variants 渲染尺寸变体，返回产出文件列表。原图不改动。"""
    parsed: list[SizeVariant] = []
    for value in variants:
        variant = parse_size_variant(value)
        if variant not in parsed:
            parsed.append(variant)
    if not parsed:
        return []

    output_dir.mkdir(parents=True, exist_ok=True)
    rendered: list[RenderedVariant] = []
    with Image.open(source) as image:
        base = image.convert("RGB")
        for variant in parsed:
            target = output_dir / f"{source.stem}_{variant}.png"
            _cover_crop(base, variant).save(target, format="PNG")
            rendered.append(
                RenderedVariant(
                    variant=str(variant),
                    path=target,
                    width=variant.width,
                    height=variant.height,
                )
            )
    return rendered


def _cover_crop(image: Image.Image, variant: SizeVariant) -> Image.Image:
    scale = max(variant.width / image.width, variant.height / image.height)
    scaled_width = max(variant.width, round(image.width * scale))
    scaled_height = max(variant.height, round(image.height * scale))
    scaled = image.resize((scaled_width, scaled_height), _RESAMPLE)
    left = (scaled_width - variant.width) // 2
    top = (scaled_height - variant.height) // 2
    return scaled.crop(
        (left, top, left + variant.width, top + variant.height)
    )
