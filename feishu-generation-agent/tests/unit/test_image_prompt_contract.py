from feishu_generation_agent.domain.reference_contract import (
    validate_image_prompt,
)


MIME_TYPES = {"asset-1": "image/png", "asset-2": "image/png"}


def _task(**updates: object) -> dict:
    task = {
        "task_id": "task-1",
        "task_type": "image_to_image",
        "prompt": (
            "@图片1 中的男性中景，脸部因愤怒而扭曲，"
            "戏剧化顶光 + 侧逆光，3D 卡通迪士尼风格"
        ),
        "reference_images": [
            {"asset_id": "asset-1", "role": "reference_image", "order": 1}
        ],
        "negative_constraints": [],
    }
    task.update(updates)
    return task


def test_valid_image_prompt_has_no_issues():
    assert validate_image_prompt(_task(), MIME_TYPES) == []


def test_missing_reference_token_is_reported():
    issues = validate_image_prompt(
        _task(prompt="一个男性中景，脸部愤怒扭曲，戏剧化顶光"), MIME_TYPES
    )
    assert any("缺少素材引用" in issue for issue in issues)


def test_leaked_internal_asset_id_is_reported():
    issues = validate_image_prompt(
        _task(
            prompt=(
                "asset-1 中的男性中景，脸部因愤怒扭曲，戏剧化顶光 + 侧逆光"
            )
        ),
        MIME_TYPES,
    )
    assert any("不得包含内部素材 ID" in issue for issue in issues)


def test_missing_lighting_description_is_reported():
    issues = validate_image_prompt(
        _task(prompt="@图片1 中的男性中景，脸部因愤怒而扭曲"), MIME_TYPES
    )
    assert any("光影" in issue for issue in issues)


def test_video_vocabulary_is_rejected():
    for vocabulary in ("运镜", "镜头运动", "音效", "配音"):
        issues = validate_image_prompt(
            _task(
                prompt=(
                    f"@图片1 中的男性中景，脸部因愤怒扭曲，"
                    f"戏剧化顶光，{vocabulary}缓慢推进"
                )
            ),
            MIME_TYPES,
        )
        assert any(
            "视频" in issue and vocabulary in issue for issue in issues
        ), f"{vocabulary} 未被拦截：{issues}"


def test_absolute_seconds_are_rejected():
    issues = validate_image_prompt(
        _task(
            prompt=(
                "@图片1 中的男性中景，脸部因愤怒扭曲，戏剧化顶光，"
                "0-3 秒保持不动"
            )
        ),
        MIME_TYPES,
    )
    assert any("秒" in issue for issue in issues)


def test_reference_order_must_be_continuous():
    issues = validate_image_prompt(
        _task(
            prompt=(
                "@图片1 与 @图片2 握手言和，明媚的光线，两人都自信"
            ),
            reference_images=[
                {"asset_id": "asset-1", "role": "reference_image", "order": 1},
                {"asset_id": "asset-2", "role": "reference_image", "order": 3},
            ],
        ),
        MIME_TYPES,
    )
    assert any("order" in issue for issue in issues)


def test_multiple_references_all_must_be_mentioned():
    issues = validate_image_prompt(
        _task(
            prompt="@图片1 中的男性中景，脸部愤怒扭曲，戏剧化顶光",
            reference_images=[
                {"asset_id": "asset-1", "role": "reference_image", "order": 1},
                {"asset_id": "asset-2", "role": "reference_image", "order": 2},
            ],
        ),
        MIME_TYPES,
    )
    assert any("@图片2" in issue for issue in issues)


def test_non_string_prompt_is_reported():
    issues = validate_image_prompt(_task(prompt=None), MIME_TYPES)
    assert issues == ["图片 prompt 必须是字符串"]


def test_unparsable_references_are_reported():
    issues = validate_image_prompt(_task(reference_images="nope"), MIME_TYPES)
    assert issues == ["图片 reference_images 必须是列表"]
