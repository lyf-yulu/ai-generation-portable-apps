"""出图必须裁到需求要求的尺寸，且必须在落盘前完成。

两个真实故障催生了这组测试：
1. image_resize 模块建好后从没接进执行链路（grep 在 src/ 零命中），
   出的图一直是模型原生尺寸。
2. 第一版接线在产物落盘「之后」另写文件，破坏了 FileStore 的
   {sha256}.{extension} 产物命名契约，verify_artifact 校验失败，
   任务被判 failed——单测全绿，只有跑执行链路才暴露。
"""

from io import BytesIO

from PIL import Image

from feishu_generation_agent.graph.nodes import _resize_image_bytes


def _png(width: int, height: int) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (width, height), (120, 80, 200)).save(buffer, format="PNG")
    return buffer.getvalue()


def _size(content: bytes) -> tuple[int, int]:
    with Image.open(BytesIO(content)) as image:
        return image.width, image.height


def test_resizes_to_requested_variant():
    resized = _resize_image_bytes(_png(2048, 2048), "1700x2500")

    assert resized is not None
    assert _size(resized) == (1700, 2500)


def test_upscales_small_source_to_target():
    resized = _resize_image_bytes(_png(512, 512), "1700x2500")

    assert resized is not None
    assert _size(resized) == (1700, 2500)


def test_returns_none_without_variant():
    content = _png(2048, 2048)

    assert _resize_image_bytes(content, None) is None
    assert _resize_image_bytes(content, "") is None


def test_skips_when_already_at_target_size():
    """已是目标尺寸时不做无谓重编码。"""
    assert _resize_image_bytes(_png(1700, 2500), "1700x2500") is None


def test_malformed_variant_is_ignored_not_fatal():
    """尺寸写错不该让整个 run 失败，出原图比不出图好。"""
    assert _resize_image_bytes(_png(2048, 2048), "huge") is None


def test_unreadable_content_is_ignored_not_fatal():
    assert _resize_image_bytes(b"not an image", "1700x2500") is None


def test_output_is_valid_png():
    """返回的字节必须能被 FileStore.validate 识别成 PNG。"""
    resized = _resize_image_bytes(_png(2048, 2048), "640x640")

    assert resized is not None
    with Image.open(BytesIO(resized)) as image:
        assert image.format == "PNG"


def test_aspect_ratio_is_preserved_by_cover_crop():
    """等比覆盖后居中裁切，不拉伸变形。"""
    resized = _resize_image_bytes(_png(1000, 1000), "500x1000")

    assert resized is not None
    assert _size(resized) == (500, 1000)
