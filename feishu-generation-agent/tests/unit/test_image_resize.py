from pathlib import Path

import pytest
from PIL import Image

from feishu_generation_agent.integrations.image_resize import (
    SizeVariant,
    parse_size_variant,
    render_size_variants,
)


def _source(tmp_path: Path, width: int = 800, height: int = 1200) -> Path:
    path = tmp_path / "source.png"
    Image.new("RGB", (width, height), (120, 80, 200)).save(path)
    return path


def test_parse_size_variant_accepts_canonical_form():
    assert parse_size_variant("1700x2500") == SizeVariant(1700, 2500)


def test_parse_size_variant_rejects_malformed_value():
    for value in ("", "big", "1700", "1700x", "0x100", "-5x10"):
        with pytest.raises(ValueError):
            parse_size_variant(value)


def test_render_writes_one_file_per_variant(tmp_path: Path):
    source = _source(tmp_path)

    rendered = render_size_variants(
        source, ["1080x2080", "1700x2500"], output_dir=tmp_path / "out"
    )

    assert [item.variant for item in rendered] == ["1080x2080", "1700x2500"]
    for item in rendered:
        assert item.path.exists()
        with Image.open(item.path) as image:
            assert f"{image.width}x{image.height}" == item.variant


def test_render_preserves_source_untouched(tmp_path: Path):
    source = _source(tmp_path, 800, 1200)

    render_size_variants(source, ["400x600"], output_dir=tmp_path / "out")

    with Image.open(source) as image:
        assert (image.width, image.height) == (800, 1200)


def test_render_covers_target_without_distortion(tmp_path: Path):
    """等比缩放后居中裁切，不拉伸变形。"""
    source = _source(tmp_path, 1000, 1000)

    rendered = render_size_variants(
        source, ["500x1000"], output_dir=tmp_path / "out"
    )

    with Image.open(rendered[0].path) as image:
        assert (image.width, image.height) == (500, 1000)


def test_render_with_no_variants_returns_empty(tmp_path: Path):
    source = _source(tmp_path)

    assert render_size_variants(source, [], output_dir=tmp_path / "out") == []


def test_render_deduplicates_repeated_variants(tmp_path: Path):
    source = _source(tmp_path)

    rendered = render_size_variants(
        source, ["800x1200", "800x1200"], output_dir=tmp_path / "out"
    )

    assert len(rendered) == 1


def test_render_rejects_malformed_variant(tmp_path: Path):
    source = _source(tmp_path)

    with pytest.raises(ValueError):
        render_size_variants(source, ["huge"], output_dir=tmp_path / "out")
