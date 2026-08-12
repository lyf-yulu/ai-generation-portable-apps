from feishu_generation_agent.domain.plan import GenerationTask
from feishu_generation_agent.graph.nodes import _execution_units


def _image_task(**updates: object) -> GenerationTask:
    payload = {
        "task_id": "task-cg",
        "task_type": "image_to_image",
        "title": "Victor 中景",
        "source_block_ids": ["block-1"],
        "user_intent": "出 CG 图",
        "prompt": "@图片1 中的男性中景，戏剧化顶光 + 侧逆光",
        "reference_images": [
            {"asset_id": "asset-1", "role": "reference_image", "order": 1}
        ],
        "aspect_ratio": "9:16",
        "image_size": "2K",
    }
    payload.update(updates)
    return GenerationTask.model_validate(payload)


def _video_task(**updates: object) -> GenerationTask:
    payload = {
        "task_id": "task-video",
        "task_type": "image_to_video",
        "title": "熊猫拉抽屉",
        "source_block_ids": ["block-1"],
        "user_intent": "完成动作",
        "prompt": "熊猫拉开抽屉",
        "reference_images": [
            {"asset_id": "asset-1", "role": "reference_image", "order": 1}
        ],
        "aspect_ratio": "9:16",
        "duration": 5,
        "resolution": "720p",
    }
    payload.update(updates)
    return GenerationTask.model_validate(payload)


def test_single_output_image_task_is_not_split():
    units = _execution_units(_image_task(output_count=1))

    assert len(units) == 1
    assert units[0].task_id == "task-cg"


def test_multi_output_image_task_splits_into_units():
    units = _execution_units(_image_task(output_count=3))

    assert [unit.task_id for unit in units] == [
        "task-cg::output:1",
        "task-cg::output:2",
        "task-cg::output:3",
    ]
    assert all(unit.output_count == 1 for unit in units)


def test_split_image_units_keep_image_fields():
    units = _execution_units(
        _image_task(
            output_count=2,
            image_provider="seedream",
            size_variants=["1080x2080", "1700x2500"],
            safe_area="1080x2080",
        )
    )

    for unit in units:
        assert unit.image_provider == "seedream"
        assert unit.size_variants == ["1080x2080", "1700x2500"]
        assert unit.safe_area == "1080x2080"
        assert unit.resolved_image_provider == "seedream"


def test_multi_output_video_task_still_splits():
    units = _execution_units(_video_task(output_count=2))

    assert [unit.task_id for unit in units] == [
        "task-video::output:1",
        "task-video::output:2",
    ]


def test_single_output_video_task_is_not_split():
    units = _execution_units(_video_task(output_count=1))

    assert len(units) == 1
    assert units[0].task_id == "task-video"
