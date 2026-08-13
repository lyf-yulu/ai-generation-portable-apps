"""出图后必须裁到需求要求的尺寸。

阶段 2 建了 image_resize 模块和测试，但从没接进执行链路（grep 在 src/
里零命中），属于死代码——出的图一直是模型原生尺寸，不是需求要的尺寸。
"""

from pathlib import Path

import pytest
from PIL import Image

from feishu_generation_agent.graph.nodes import _resize_artifact_to_variant


def _image(path: Path, width: int, height: int) -> Path:
    Image.new("RGB", (width, height), (120, 80, 200)).save(path)
    return path


def test_resizes_to_requested_variant(tmp_path: Path):
    source = _image(tmp_path / "out.png", 2048, 2048)

    resized = _resize_artifact_to_variant(source, "1700x2500")

    assert resized is not None
    with Image.open(resized) as image:
        assert (image.width, image.height) == (1700, 2500)


def test_returns_none_without_variant(tmp_path: Path):
    source = _image(tmp_path / "out.png", 2048, 2048)

    assert _resize_artifact_to_variant(source, None) is None
    assert _resize_artifact_to_variant(source, "") is None


def test_skips_when_already_at_target_size(tmp_path: Path):
    source = _image(tmp_path / "out.png", 1700, 2500)

    assert _resize_artifact_to_variant(source, "1700x2500") is None


def test_malformed_variant_is_ignored_not_fatal(tmp_path: Path):
    """尺寸写错不该让整个 run 失败，出原图比不出图好。"""
    source = _image(tmp_path / "out.png", 2048, 2048)

    assert _resize_artifact_to_variant(source, "huge") is None


def test_unreadable_source_is_ignored_not_fatal(tmp_path: Path):
    missing = tmp_path / "nope.png"

    assert _resize_artifact_to_variant(missing, "1700x2500") is None


def test_original_file_is_left_untouched(tmp_path: Path):
    source = _image(tmp_path / "out.png", 2048, 2048)

    _resize_artifact_to_variant(source, "1700x2500")

    with Image.open(source) as image:
        assert (image.width, image.height) == (2048, 2048)
