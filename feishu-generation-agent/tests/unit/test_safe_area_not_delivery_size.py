"""安全区不能出现在 size_variants 里。

真实故障：模型把安全区 1080x2080 当成交付尺寸，产出
size_variants=['1080x2080'] 或 ['1080x2080','1700x2500']，导致裁出安全区
尺寸的图、甚至按尺寸把一个概念拆成多个任务（实测 2 个概念被拆成 4 个）。

契约已写明安全区是构图界限，但模型反复不遵守。改在领域层直接过滤——
确定性规则不该依赖模型听话。
"""

from feishu_generation_agent.domain.plan import GenerationTask


def _payload(**updates: object) -> dict:
    payload = {
        "task_id": "cg-1",
        "task_type": "image_to_image",
        "title": "Victor 中景",
        "source_block_ids": ["b1"],
        "user_intent": "出 CG 图",
        "prompt": "@图片1 中的男性中景，戏剧化顶光",
        "reference_images": [
            {"asset_id": "a1", "role": "reference_image", "order": 1}
        ],
        "aspect_ratio": "9:16",
        "image_size": "2K",
    }
    payload.update(updates)
    return payload


def test_safe_area_is_dropped_from_variants():
    task = GenerationTask.model_validate(
        _payload(
            safe_area="1080x2080",
            size_variants=["1080x2080", "1700x2500"],
        )
    )

    assert task.size_variants == ["1700x2500"]


def test_safe_area_only_variant_is_dropped_entirely():
    """只填了安全区时也要剔除，剩下的交付尺寸由 image_size 兜底。"""
    task = GenerationTask.model_validate(
        _payload(safe_area="1080x2080", size_variants=["1080x2080"])
    )

    assert task.size_variants == []


def test_safe_area_matching_is_separator_insensitive():
    task = GenerationTask.model_validate(
        _payload(safe_area="1080*2080", size_variants=["1080x2080"])
    )

    assert task.size_variants == []


def test_delivery_sizes_are_kept():
    task = GenerationTask.model_validate(
        _payload(safe_area="1080x2080", size_variants=["1700x2500"])
    )

    assert task.size_variants == ["1700x2500"]


def test_no_safe_area_keeps_all_variants():
    task = GenerationTask.model_validate(
        _payload(size_variants=["1080x2080", "1700x2500"])
    )

    assert task.size_variants == ["1080x2080", "1700x2500"]


def test_safe_area_itself_is_preserved():
    """剔除只作用于 size_variants，safe_area 字段本身要保留给 prompt 用。"""
    task = GenerationTask.model_validate(
        _payload(safe_area="1080x2080", size_variants=["1700x2500"])
    )

    assert task.safe_area == "1080x2080"
