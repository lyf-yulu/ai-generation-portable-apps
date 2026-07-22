import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from feishu_generation_agent.domain.document import (
    DocumentBlock,
    MediaAsset,
    NormalizedDocument,
    SourceType,
    VisionDescription,
)
from feishu_generation_agent.domain.errors import AgentError, ErrorCategory
from feishu_generation_agent.domain.plan import AuditReport, TaskPlan
from feishu_generation_agent.integrations.planner import (
    DeepSeekPlanner,
    validate_plan,
)


def _asset(
    tmp_path: Path,
    asset_id: str,
    source_block_id: str,
    *,
    mime_type: str = "image/png",
    download_error: str | None = None,
) -> MediaAsset:
    path = tmp_path / f"{asset_id}.png"
    if download_error is None:
        path.write_bytes(b"fictional-image")
    return MediaAsset(
        asset_id=asset_id,
        source_block_id=source_block_id,
        origin="feishu",
        local_path=path,
        mime_type=mime_type,
        size=path.stat().st_size if path.exists() else 0,
        sha256=f"sha-{asset_id}" if path.exists() else "",
        download_error=download_error,
    )


def _vision(asset_id: str) -> VisionDescription:
    return VisionDescription(
        asset_id=asset_id,
        subjects=["蓝色纸船"],
        scene="虚构的小河",
        style="柔和插画",
        composition="纸船位于画面中央",
        characters=[],
        actions=["纸船向前漂流"],
        visible_text=[],
        colors=["蓝色", "绿色"],
        probable_role="场景与主体参考图",
        uncertainties=["无法确认水流速度"],
    )


@pytest.fixture
def narrative_document(tmp_path: Path) -> NormalizedDocument:
    asset = _asset(tmp_path, "asset-1", "image-1")
    return NormalizedDocument(
        document_id="doc-narrative",
        title="纸船短片需求",
        revision=3,
        source_type=SourceType.DOCX,
        source_token="doc-narrative",
        blocks=[
            DocumentBlock(
                block_id="page-1",
                parent_id=None,
                block_type="page",
                order=0,
                path=["page-1"],
                text="纸船短片需求",
            ),
            DocumentBlock(
                block_id="story-1",
                parent_id="page-1",
                block_type="text",
                order=1,
                path=["page-1", "story-1"],
                text="让纸船从近景漂向远处，形成一个连续视频。",
            ),
            DocumentBlock(
                block_id="image-1",
                parent_id="page-1",
                block_type="image",
                order=2,
                path=["page-1", "image-1"],
                image_asset_id="asset-1",
            ),
        ],
        text_view=(
            "[block:story-1] 让纸船从近景漂向远处，形成一个连续视频。\n"
            "[block:image-1] [image:asset-1]"
        ),
        media_assets=[asset],
    )


@pytest.fixture
def storyboard_document(tmp_path: Path) -> NormalizedDocument:
    asset = _asset(tmp_path, "asset-1", "image-1")
    blocks = [
        DocumentBlock(
            block_id="page-1",
            parent_id=None,
            block_type="page",
            order=0,
            path=["page-1"],
            text="纸船分镜表",
        ),
        DocumentBlock(
            block_id="table-1",
            parent_id="page-1",
            block_type="table",
            order=1,
            path=["page-1", "table-1"],
        ),
    ]
    text_lines = []
    for row in range(4):
        cell_id = f"cell-{row}"
        shot_id = f"shot-{row + 1}"
        shot_text = f"镜头 {row + 1}：纸船经过虚构场景 {row + 1}。"
        blocks.extend(
            [
                DocumentBlock(
                    block_id=cell_id,
                    parent_id="table-1",
                    block_type="table_cell",
                    order=2 + row * 2,
                    path=["page-1", "table-1", cell_id],
                    table_row=row,
                    table_column=0,
                ),
                DocumentBlock(
                    block_id=shot_id,
                    parent_id=cell_id,
                    block_type="text",
                    order=3 + row * 2,
                    path=["page-1", "table-1", cell_id, shot_id],
                    text=shot_text,
                ),
            ]
        )
        text_lines.append(f"[block:{shot_id}] {shot_text}")
    blocks.append(
        DocumentBlock(
            block_id="image-1",
            parent_id="page-1",
            block_type="image",
            order=10,
            path=["page-1", "image-1"],
            image_asset_id="asset-1",
        )
    )
    text_lines.append("[block:image-1] [image:asset-1]")
    return NormalizedDocument(
        document_id="doc-storyboard",
        title="纸船分镜表",
        revision=5,
        source_type=SourceType.DOCX,
        source_token="doc-storyboard",
        blocks=blocks,
        text_view="\n".join(text_lines),
        media_assets=[asset],
    )


def _with_storyboard_header(
    document: NormalizedDocument,
) -> NormalizedDocument:
    shifted_blocks = [
        block.model_copy(update={"table_row": block.table_row + 1})
        if block.block_type == "table_cell"
        and block.parent_id == "table-1"
        and block.table_row is not None
        else block
        for block in document.blocks
    ]
    header_blocks = [
        DocumentBlock(
            block_id="header-cell",
            parent_id="table-1",
            block_type="table_cell",
            order=2,
            path=["page-1", "table-1", "header-cell"],
            table_row=0,
            table_column=0,
        ),
        DocumentBlock(
            block_id="header-title",
            parent_id="header-cell",
            block_type="text",
            order=3,
            path=["page-1", "table-1", "header-cell", "header-title"],
            text="画面描述",
        ),
    ]
    return document.model_copy(
        update={
            "blocks": [*shifted_blocks, *header_blocks],
            "text_view": (
                "[block:header-title] 画面描述\n" + document.text_view
            ),
        }
    )


def _numbered_storyboard_document(
    document: NormalizedDocument,
    *,
    header: str,
    numbers: list[str],
) -> NormalizedDocument:
    blocks = [
        block
        for block in document.blocks
        if block.block_id in {"page-1", "table-1", "image-1"}
    ]
    text_lines = [f"[block:number-header] {header}"]
    blocks.extend(
        [
            DocumentBlock(
                block_id="number-header-cell",
                parent_id="table-1",
                block_type="table_cell",
                order=2,
                path=["page-1", "table-1", "number-header-cell"],
                table_row=0,
                table_column=0,
            ),
            DocumentBlock(
                block_id="number-header",
                parent_id="number-header-cell",
                block_type="text",
                order=3,
                path=[
                    "page-1",
                    "table-1",
                    "number-header-cell",
                    "number-header",
                ],
                text=header,
            ),
            DocumentBlock(
                block_id="description-header-cell",
                parent_id="table-1",
                block_type="table_cell",
                order=4,
                path=["page-1", "table-1", "description-header-cell"],
                table_row=0,
                table_column=1,
            ),
            DocumentBlock(
                block_id="description-header",
                parent_id="description-header-cell",
                block_type="text",
                order=5,
                path=[
                    "page-1",
                    "table-1",
                    "description-header-cell",
                    "description-header",
                ],
                text="画面描述",
            ),
        ]
    )
    for row, number in enumerate(numbers, start=1):
        number_cell = f"number-cell-{row}"
        number_id = f"shot-number-{row}"
        description_cell = f"description-cell-{row}"
        shot_id = f"shot-{row}"
        order = 6 + (row - 1) * 4
        blocks.extend(
            [
                DocumentBlock(
                    block_id=number_cell,
                    parent_id="table-1",
                    block_type="table_cell",
                    order=order,
                    path=["page-1", "table-1", number_cell],
                    table_row=row,
                    table_column=0,
                ),
                DocumentBlock(
                    block_id=number_id,
                    parent_id=number_cell,
                    block_type="text",
                    order=order + 1,
                    path=["page-1", "table-1", number_cell, number_id],
                    text=number,
                ),
                DocumentBlock(
                    block_id=description_cell,
                    parent_id="table-1",
                    block_type="table_cell",
                    order=order + 2,
                    path=["page-1", "table-1", description_cell],
                    table_row=row,
                    table_column=1,
                ),
                DocumentBlock(
                    block_id=shot_id,
                    parent_id=description_cell,
                    block_type="text",
                    order=order + 3,
                    path=["page-1", "table-1", description_cell, shot_id],
                    text=f"纸船经过虚构场景 {row}。",
                ),
            ]
        )
        text_lines.extend(
            [f"[block:{number_id}] {number}", f"[block:{shot_id}] 场景 {row}"]
        )
    return document.model_copy(
        update={"blocks": blocks, "text_view": "\n".join(text_lines)}
    )


@pytest.fixture
def mixed_document(tmp_path: Path) -> NormalizedDocument:
    first = _asset(tmp_path, "asset-1", "image-1")
    second = _asset(tmp_path, "asset-2", "image-2")
    return NormalizedDocument(
        document_id="doc-mixed",
        title="海报与短片",
        revision=2,
        source_type=SourceType.DOCX,
        source_token="doc-mixed",
        blocks=[
            DocumentBlock(
                block_id="image-request",
                parent_id=None,
                block_type="text",
                order=0,
                path=["image-request"],
                text="根据素材一生成竖版海报。",
            ),
            DocumentBlock(
                block_id="video-request",
                parent_id=None,
                block_type="text",
                order=1,
                path=["video-request"],
                text="根据素材二生成横版短片。",
            ),
            DocumentBlock(
                block_id="image-1",
                parent_id=None,
                block_type="image",
                order=2,
                path=["image-1"],
                image_asset_id="asset-1",
            ),
            DocumentBlock(
                block_id="image-2",
                parent_id=None,
                block_type="image",
                order=3,
                path=["image-2"],
                image_asset_id="asset-2",
            ),
        ],
        text_view=(
            "[block:image-request] 根据 [image:asset-1] 生成竖版海报。\n"
            "[block:video-request] 根据 [image:asset-2] 生成横版短片。"
        ),
        media_assets=[first, second],
    )


@pytest.fixture
def vision_descriptions() -> list[VisionDescription]:
    return [_vision("asset-1")]


def _video_task(
    task_id: str = "task-video",
    *,
    source_block_ids: list[str] | None = None,
    asset_id: str = "asset-1",
    output_count: int = 1,
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "task_type": "image_to_video",
        "title": "纸船漂流短片",
        "source_block_ids": source_block_ids or ["story-1"],
        "user_intent": "生成连续的纸船漂流视频",
        "prompt": "纸船从近景漂向远处",
        "reference_images": [
            {"asset_id": asset_id, "role": "reference_image", "order": 1}
        ],
        "aspect_ratio": "16:9",
        "duration": 10,
        "resolution": "720p",
        "generate_audio": False,
        "output_count": output_count,
        "confidence": 0.9,
    }


def _image_task(task_id: str = "task-image") -> dict[str, Any]:
    return {
        "task_id": task_id,
        "task_type": "image_to_image",
        "title": "纸船海报",
        "source_block_ids": ["image-request"],
        "user_intent": "生成竖版纸船海报",
        "prompt": "蓝色纸船的竖版海报",
        "reference_images": [
            {"asset_id": "asset-1", "role": "reference_image", "order": 1}
        ],
        "aspect_ratio": "9:16",
        "image_size": "2K",
        "output_count": 1,
        "confidence": 0.9,
    }


def _plan_json(*tasks: dict[str, Any]) -> str:
    return json.dumps(
        {"tasks": list(tasks), "document_summary": "测试生成需求"},
        ensure_ascii=False,
    )


class FakeDeepSeekModel:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.calls = 0
        self.bind_calls: list[dict[str, Any]] = []
        self.requests: list[list[dict[str, Any]]] = []
        self.api_key = "fictional-deepseek-key-must-not-leak"

    def bind(self, **kwargs: Any) -> "FakeDeepSeekModel":
        self.bind_calls.append(kwargs)
        return self

    async def ainvoke(self, messages: list[dict[str, Any]]) -> object:
        self.calls += 1
        self.requests.append(copy.deepcopy(messages))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return SimpleNamespace(
            content=response,
            additional_kwargs={
                "reasoning_content": "fictional private chain of thought"
            },
        )


class RateLimitFailure(RuntimeError):
    status_code = 429


async def test_storyboard_rows_become_one_video_task(
    storyboard_document: NormalizedDocument,
    vision_descriptions: list[VisionDescription],
):
    task = _video_task(
        source_block_ids=[f"shot-{index}" for index in range(1, 5)]
    )
    task["prompt"] = "；".join(
        f"镜头 {index}：纸船经过场景 {index}" for index in range(1, 5)
    )
    model = FakeDeepSeekModel([_plan_json(task)])
    planner = DeepSeekPlanner(model, max_output_count=4)

    plan = await planner.plan(
        storyboard_document,
        vision_descriptions,
        feedback=None,
    )

    assert len(plan.tasks) == 1
    assert plan.tasks[0].task_type == "image_to_video"
    assert "镜头 1" in plan.tasks[0].prompt
    assert "镜头 4" in plan.tasks[0].prompt


async def test_planning_input_contains_stable_document_and_rules(
    storyboard_document: NormalizedDocument,
    vision_descriptions: list[VisionDescription],
):
    task = _video_task(
        source_block_ids=[f"shot-{index}" for index in range(1, 5)]
    )
    model = FakeDeepSeekModel([_plan_json(task)])
    planner = DeepSeekPlanner(model, max_output_count=4)

    await planner.plan(storyboard_document, vision_descriptions, feedback="保留蓝色")

    assert model.bind_calls == [
        {
            "response_format": {"type": "json_object"},
            "extra_body": {
                "thinking": {"type": "enabled"},
                "reasoning_effort": "high",
            },
        }
    ]
    request = model.requests[0]
    user_prompt = request[1]["content"]
    assert storyboard_document.text_view in user_prompt
    assert '"table_row":0' in user_prompt
    assert '"block_id":"shot-1"' in user_prompt
    assert '"asset_id":"asset-1"' in user_prompt
    assert '"scene":"虚构的小河"' in user_prompt
    assert "image_to_image" in user_prompt
    assert "image_to_video" in user_prompt
    assert json.dumps(
        TaskPlan.model_json_schema(),
        ensure_ascii=False,
        separators=(",", ":"),
    ) in user_prompt
    assert "图片匹配优先级" in user_prompt
    assert "同一分镜表" in user_prompt and "一个视频任务" in user_prompt
    assert "保留蓝色" in user_prompt


async def test_planning_prompt_does_not_send_download_error_detail(
    narrative_document: NormalizedDocument,
    vision_descriptions: list[VisionDescription],
):
    secret = "fictional-secret-in-download-error"
    failed_asset = narrative_document.media_assets[0].model_copy(
        update={"download_error": secret}
    )
    failed_document = narrative_document.model_copy(
        update={"media_assets": [failed_asset]}
    )
    model = FakeDeepSeekModel(
        [_plan_json(_video_task()), _plan_json(_video_task())]
    )
    planner = DeepSeekPlanner(model, max_output_count=4)

    with pytest.raises(AgentError):
        await planner.plan(failed_document, vision_descriptions)

    user_prompt = model.requests[0][1]["content"]
    assert secret not in user_prompt
    assert '"download_succeeded":false' in user_prompt


async def test_free_narrative_and_mixed_tasks_are_supported(
    narrative_document: NormalizedDocument,
    mixed_document: NormalizedDocument,
    vision_descriptions: list[VisionDescription],
):
    narrative_model = FakeDeepSeekModel([_plan_json(_video_task())])
    narrative_planner = DeepSeekPlanner(narrative_model, max_output_count=4)
    narrative_plan = await narrative_planner.plan(
        narrative_document,
        vision_descriptions,
    )

    mixed_video = _video_task(
        source_block_ids=["video-request"], asset_id="asset-2"
    )
    mixed_model = FakeDeepSeekModel([_plan_json(_image_task(), mixed_video)])
    mixed_planner = DeepSeekPlanner(mixed_model, max_output_count=4)
    mixed_plan = await mixed_planner.plan(
        mixed_document,
        [_vision("asset-1"), _vision("asset-2")],
    )

    assert [task.task_type.value for task in narrative_plan.tasks] == [
        "image_to_video"
    ]
    assert [task.task_type.value for task in mixed_plan.tasks] == [
        "image_to_image",
        "image_to_video",
    ]


async def test_invalid_json_is_repaired_once(
    narrative_document: NormalizedDocument,
    vision_descriptions: list[VisionDescription],
):
    model = FakeDeepSeekModel(["not-json", _plan_json(_video_task())])
    planner = DeepSeekPlanner(model, max_output_count=4)

    await planner.plan(narrative_document, vision_descriptions, feedback=None)

    assert model.calls == 2
    assert len(model.requests[1]) == len(model.requests[0]) + 1
    repair_prompt = model.requests[1][-1]["content"]
    assert "not-json" in repair_prompt
    assert "校验错误" in repair_prompt
    assert "fictional-deepseek-key-must-not-leak" not in repair_prompt


async def test_second_invalid_response_raises_safe_error_without_third_call(
    narrative_document: NormalizedDocument,
    vision_descriptions: list[VisionDescription],
):
    model = FakeDeepSeekModel(["not-json-first", "not-json-second"])
    planner = DeepSeekPlanner(model, max_output_count=4)

    with pytest.raises(AgentError) as raised:
        await planner.plan(narrative_document, vision_descriptions)

    assert model.calls == 2
    detail = raised.value.detail
    serialized = json.dumps(detail.model_dump(mode="json"))
    assert detail.category == ErrorCategory.VALIDATION
    assert detail.retryable is False
    assert narrative_document.document_id in serialized
    assert "not-json" not in serialized
    assert model.api_key not in serialized
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


async def test_non_string_task_type_is_repaired_once(
    narrative_document: NormalizedDocument,
    vision_descriptions: list[VisionDescription],
):
    invalid = json.loads(_plan_json(_video_task()))
    invalid["tasks"][0]["task_type"] = []
    model = FakeDeepSeekModel(
        [json.dumps(invalid, ensure_ascii=False), _plan_json(_video_task())]
    )
    planner = DeepSeekPlanner(model, max_output_count=4)

    plan = await planner.plan(narrative_document, vision_descriptions)

    assert len(plan.tasks) == 1
    assert model.calls == 2
    assert "task_type" in model.requests[1][-1]["content"]


async def test_two_non_string_task_types_raise_safe_validation_error(
    narrative_document: NormalizedDocument,
    vision_descriptions: list[VisionDescription],
):
    first = json.loads(_plan_json(_video_task()))
    first["tasks"][0]["task_type"] = []
    second = json.loads(_plan_json(_video_task()))
    second["tasks"][0]["task_type"] = {}
    model = FakeDeepSeekModel(
        [
            json.dumps(first, ensure_ascii=False),
            json.dumps(second, ensure_ascii=False),
        ]
    )
    planner = DeepSeekPlanner(model, max_output_count=4)

    with pytest.raises(AgentError) as raised:
        await planner.plan(narrative_document, vision_descriptions)

    assert model.calls == 2
    detail = raised.value.detail
    serialized = json.dumps(detail.model_dump(mode="json"))
    assert detail.category == ErrorCategory.VALIDATION
    assert detail.retryable is False
    assert "fictional private chain of thought" not in serialized
    assert model.api_key not in serialized
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


async def test_empty_plan_is_repaired_once(
    narrative_document: NormalizedDocument,
    vision_descriptions: list[VisionDescription],
):
    model = FakeDeepSeekModel([_plan_json(), _plan_json(_video_task())])
    planner = DeepSeekPlanner(model, max_output_count=4)

    plan = await planner.plan(narrative_document, vision_descriptions)

    assert len(plan.tasks) == 1
    assert model.calls == 2
    assert "at least one generation task" in model.requests[1][-1]["content"]


async def test_two_empty_plans_raise_safe_validation_error(
    narrative_document: NormalizedDocument,
    vision_descriptions: list[VisionDescription],
):
    model = FakeDeepSeekModel([_plan_json(), _plan_json()])
    planner = DeepSeekPlanner(model, max_output_count=4)

    with pytest.raises(AgentError) as raised:
        await planner.plan(narrative_document, vision_descriptions)

    assert model.calls == 2
    detail = raised.value.detail
    serialized = json.dumps(detail.model_dump(mode="json"))
    assert detail.category == ErrorCategory.VALIDATION
    assert detail.retryable is False
    assert "fictional private chain of thought" not in serialized
    assert model.api_key not in serialized
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ConnectError(
            "fictional-secret-connect",
            request=httpx.Request("POST", "https://deepseek.invalid"),
        ),
        RateLimitFailure("fictional-secret-rate-limit"),
    ],
)
async def test_model_transport_errors_are_retryable_and_safe(
    narrative_document: NormalizedDocument,
    vision_descriptions: list[VisionDescription],
    failure: Exception,
):
    model = FakeDeepSeekModel([failure])
    planner = DeepSeekPlanner(model, max_output_count=4)

    with pytest.raises(AgentError) as raised:
        await planner.plan(narrative_document, vision_descriptions)

    assert model.calls == 1
    detail = raised.value.detail
    serialized = json.dumps(detail.model_dump(mode="json"))
    assert detail.category == ErrorCategory.TRANSIENT
    assert detail.retryable is True
    assert narrative_document.document_id in serialized
    assert "fictional-secret" not in serialized
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_validator_accepts_task_plan_and_valid_raw_plan(
    narrative_document: NormalizedDocument,
):
    raw_plan = json.loads(_plan_json(_video_task()))
    typed_plan = TaskPlan.model_validate(raw_plan)

    assert validate_plan(raw_plan, narrative_document, 4) == []
    assert validate_plan(typed_plan, narrative_document, 4) == []


def test_validator_rejects_frame_mode_without_exactly_two_frame_roles(
    narrative_document: NormalizedDocument,
):
    raw_plan = json.loads(_plan_json(_video_task()))
    raw_plan["tasks"][0].update(reference_mode="first_last_frame")
    raw_plan["tasks"][0]["reference_images"] = [
        {"asset_id": "asset-1", "role": "first_frame", "order": 1}
    ]

    issues = validate_plan(raw_plan, narrative_document, 4)

    assert "首尾帧模式" in " ".join(issues)


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (lambda plan: plan["tasks"][0].update(task_type="text_to_video"), "task_type"),
        (
            lambda plan: plan["tasks"][0].update(reference_images=[]),
            "reference_images",
        ),
        (
            lambda plan: plan["tasks"][0]["reference_images"][0].update(
                asset_id="missing"
            ),
            "unknown asset_id",
        ),
        (
            lambda plan: plan["tasks"][0].update(source_block_ids=["missing"]),
            "source_block_ids",
        ),
        (
            lambda plan: plan["tasks"][0].update(image_size="2K"),
            "image_size",
        ),
        (
            lambda plan: plan["tasks"][0].pop("duration"),
            "duration",
        ),
    ],
)
def test_validator_reports_stable_raw_plan_issues(
    narrative_document: NormalizedDocument,
    mutation: Any,
    expected: str,
):
    raw_plan = json.loads(_plan_json(_video_task()))
    mutation(raw_plan)

    issues = validate_plan(raw_plan, narrative_document, 4)

    assert expected in " ".join(issues)
    assert issues == validate_plan(raw_plan, narrative_document, 4)


@pytest.mark.parametrize("raw_task_type", [[], {}, None, ""])
def test_validator_handles_non_string_or_empty_task_type(
    narrative_document: NormalizedDocument,
    raw_task_type: object,
):
    raw_plan = json.loads(_plan_json(_video_task()))
    raw_plan["tasks"][0]["task_type"] = raw_task_type

    issues = validate_plan(raw_plan, narrative_document, 4)

    assert "tasks[0].task_type" in " ".join(issues)
    assert issues == validate_plan(raw_plan, narrative_document, 4)


@pytest.mark.parametrize(
    "raw_plan",
    [
        {"unexpected": {}},
        {"tasks": [[]]},
        {
            "tasks": [
                {
                    "task_id": {},
                    "task_type": "image_to_video",
                    "source_block_ids": [{}],
                    "reference_images": [{"asset_id": {}}],
                    "duration": {},
                    "resolution": [],
                    "generate_audio": [],
                    "output_count": {},
                }
            ]
        },
    ],
)
def test_validator_returns_issues_for_arbitrary_json_objects(
    narrative_document: NormalizedDocument,
    raw_plan: dict[str, Any],
):
    issues = validate_plan(raw_plan, narrative_document, 4)

    assert issues
    assert issues == validate_plan(raw_plan, narrative_document, 4)


def test_validator_rejects_empty_generation_plan(
    narrative_document: NormalizedDocument,
):
    raw_plan = json.loads(_plan_json())

    issues = validate_plan(raw_plan, narrative_document, 4)

    assert issues == ["plan.tasks: at least one generation task is required"]


def test_validator_rejects_failed_or_non_image_assets(
    narrative_document: NormalizedDocument,
):
    failed = narrative_document.media_assets[0].model_copy(
        update={"download_error": "fictional failure"}
    )
    failed_document = narrative_document.model_copy(
        update={"media_assets": [failed]}
    )
    non_image = narrative_document.media_assets[0].model_copy(
        update={"mime_type": "video/mp4"}
    )
    non_image_document = narrative_document.model_copy(
        update={"media_assets": [non_image]}
    )
    raw_plan = json.loads(_plan_json(_video_task()))

    assert "download" in " ".join(validate_plan(raw_plan, failed_document, 4))
    assert "image MIME" in " ".join(
        validate_plan(raw_plan, non_image_document, 4)
    )


def test_validator_rejects_asset_whose_local_file_is_missing(
    narrative_document: NormalizedDocument,
    tmp_path: Path,
):
    missing = narrative_document.media_assets[0].model_copy(
        update={"local_path": tmp_path / "missing.png"}
    )
    missing_document = narrative_document.model_copy(
        update={"media_assets": [missing]}
    )
    raw_plan = json.loads(_plan_json(_video_task()))

    issues = validate_plan(raw_plan, missing_document, 4)

    assert "download" in " ".join(issues)


def test_validator_checks_total_output_count(
    narrative_document: NormalizedDocument,
):
    first = _video_task("task-1", output_count=3)
    second = _video_task("task-2", output_count=2)
    raw_plan = json.loads(_plan_json(first, second))

    issues = validate_plan(raw_plan, narrative_document, 4)

    assert "total output_count" in " ".join(issues)


def test_validator_requires_storyboard_rows_to_merge(
    storyboard_document: NormalizedDocument,
):
    first = _video_task("task-1", source_block_ids=["shot-1"])
    second = _video_task("task-2", source_block_ids=["shot-2"])
    raw_plan = json.loads(_plan_json(first, second))

    issues = validate_plan(raw_plan, storyboard_document, 4)

    assert "storyboard" in " ".join(issues)
    assert "exactly one image_to_video" in " ".join(issues)


def test_validator_accepts_one_video_covering_every_storyboard_row(
    storyboard_document: NormalizedDocument,
):
    task = _video_task(
        source_block_ids=[f"shot-{index}" for index in range(1, 5)]
    )

    assert validate_plan(
        json.loads(_plan_json(task)), storyboard_document, 4
    ) == []


def test_validator_rejects_one_video_missing_storyboard_rows(
    storyboard_document: NormalizedDocument,
):
    task = _video_task(source_block_ids=["shot-1"])

    issues = validate_plan(
        json.loads(_plan_json(task)), storyboard_document, 4
    )

    joined = " ".join(issues)
    assert "storyboard table table-1" in joined
    assert "missing source_block_ids" in joined
    assert "shot-2" in joined and "shot-3" in joined and "shot-4" in joined


def test_validator_requires_every_content_block_in_storyboard_rows(
    storyboard_document: NormalizedDocument,
):
    detail = DocumentBlock(
        block_id="shot-detail-1",
        parent_id="cell-0",
        block_type="text",
        order=4,
        path=["page-1", "table-1", "cell-0", "shot-detail-1"],
        text="持续 2 秒，画面保持稳定。",
    )
    document = storyboard_document.model_copy(
        update={"blocks": [*storyboard_document.blocks, detail]}
    )
    base_sources = [f"shot-{index}" for index in range(1, 5)]

    incomplete = validate_plan(
        json.loads(_plan_json(_video_task(source_block_ids=base_sources))),
        document,
        4,
    )
    complete = validate_plan(
        json.loads(
            _plan_json(
                _video_task(
                    source_block_ids=[*base_sources, "shot-detail-1"]
                )
            )
        ),
        document,
        4,
    )

    assert "shot-detail-1" in " ".join(incomplete)
    assert complete == []


def test_validator_rejects_image_task_for_storyboard_rows(
    storyboard_document: NormalizedDocument,
):
    task = _image_task()
    task["source_block_ids"] = [f"shot-{index}" for index in range(1, 5)]

    issues = validate_plan(
        json.loads(_plan_json(task)), storyboard_document, 4
    )

    joined = " ".join(issues)
    assert "storyboard table table-1" in joined
    assert "must be image_to_video" in joined


def test_validator_rejects_storyboard_split_across_image_and_video_tasks(
    storyboard_document: NormalizedDocument,
):
    image = _image_task("task-image")
    image["source_block_ids"] = ["shot-1"]
    video = _video_task(
        "task-video", source_block_ids=["shot-2", "shot-3", "shot-4"]
    )

    issues = validate_plan(
        json.loads(_plan_json(image, video)), storyboard_document, 4
    )

    joined = " ".join(issues)
    assert "storyboard table table-1" in joined
    assert "exactly one image_to_video" in joined
    assert "found 2" in joined


def test_validator_does_not_treat_ordinary_table_as_storyboard(
    storyboard_document: NormalizedDocument,
):
    ordinary_blocks = [
        block.model_copy(
            update={
                "text": block.text.replace("镜头", "参数")
                if block.text
                else block.text
            }
        )
        for block in storyboard_document.blocks
    ]
    ordinary_document = storyboard_document.model_copy(
        update={
            "title": "渲染参数表",
            "blocks": ordinary_blocks,
            "text_view": storyboard_document.text_view.replace("镜头", "参数"),
        }
    )
    task = _image_task()
    task["source_block_ids"] = ["shot-1"]

    issues = validate_plan(
        json.loads(_plan_json(task)), ordinary_document, 4
    )

    assert not any("storyboard" in issue for issue in issues)


def test_validator_recognizes_explicit_storyboard_rows_after_header(
    storyboard_document: NormalizedDocument,
):
    document = _with_storyboard_header(storyboard_document)
    incomplete = validate_plan(
        json.loads(
            _plan_json(_video_task(source_block_ids=["shot-1"]))
        ),
        document,
        4,
    )
    split = validate_plan(
        json.loads(
            _plan_json(
                _video_task("task-1", source_block_ids=["shot-1"]),
                _video_task(
                    "task-2",
                    source_block_ids=["shot-2", "shot-3", "shot-4"],
                ),
            )
        ),
        document,
        4,
    )
    complete = validate_plan(
        json.loads(
            _plan_json(
                _video_task(
                    source_block_ids=[
                        "shot-1",
                        "shot-2",
                        "shot-3",
                        "shot-4",
                    ]
                )
            )
        ),
        document,
        4,
    )

    assert "missing source_block_ids" in " ".join(incomplete)
    assert "header-title" not in " ".join(incomplete)
    assert "exactly one image_to_video" in " ".join(split)
    assert complete == []


@pytest.mark.parametrize(
    ("header", "numbers"),
    [
        ("镜头", ["1", "2"]),
        ("镜号", ["1、", "2、"]),
        ("镜头号", ["1.", "2."]),
    ],
)
def test_validator_recognizes_numbered_storyboard_under_header(
    storyboard_document: NormalizedDocument,
    header: str,
    numbers: list[str],
):
    document = _numbered_storyboard_document(
        storyboard_document,
        header=header,
        numbers=numbers,
    )
    first_row_sources = ["shot-number-1", "shot-1"]
    all_row_sources = [
        "shot-number-1",
        "shot-1",
        "shot-number-2",
        "shot-2",
    ]

    incomplete = validate_plan(
        json.loads(
            _plan_json(_video_task(source_block_ids=first_row_sources))
        ),
        document,
        4,
    )
    complete = validate_plan(
        json.loads(
            _plan_json(_video_task(source_block_ids=all_row_sources))
        ),
        document,
        4,
    )

    joined = " ".join(incomplete)
    assert "storyboard table table-1" in joined
    assert "shot-number-2" in joined and "shot-2" in joined
    assert "number-header" not in joined
    assert "description-header" not in joined
    assert complete == []


@pytest.mark.parametrize(
    ("header", "numbers"),
    [
        ("镜头", ["1"]),
        ("镜头", ["1", "3"]),
        ("参数", ["1", "2"]),
    ],
)
def test_validator_ignores_incidental_header_or_scattered_numbers(
    storyboard_document: NormalizedDocument,
    header: str,
    numbers: list[str],
):
    document = _numbered_storyboard_document(
        storyboard_document,
        header=header,
        numbers=numbers,
    )
    task = _image_task()
    task["source_block_ids"] = [
        block.block_id
        for block in document.blocks
        if block.block_id.startswith(("shot-number-", "shot-"))
    ]

    issues = validate_plan(json.loads(_plan_json(task)), document, 4)

    assert not any("storyboard" in issue for issue in issues)


async def test_audit_uses_independent_prompt_and_does_not_rewrite_plan(
    narrative_document: NormalizedDocument,
    vision_descriptions: list[VisionDescription],
):
    plan_json = _plan_json(_video_task())
    audit_json = json.dumps(
        {
            "issues": ["遗漏：没有明确首尾帧关系"],
            "corrections_required": True,
        },
        ensure_ascii=False,
    )
    model = FakeDeepSeekModel([plan_json, audit_json])
    planner = DeepSeekPlanner(model, max_output_count=4)
    plan = await planner.plan(narrative_document, vision_descriptions)

    report = await planner.audit(narrative_document, plan)

    assert report == AuditReport(
        issues=["遗漏：没有明确首尾帧关系"],
        corrections_required=True,
    )
    planning_system = model.requests[0][0]["content"]
    audit_system = model.requests[1][0]["content"]
    assert planning_system != audit_system
    assert "独立审查" in audit_system
    assert "遗漏" in audit_system
    assert "冲突" in audit_system
    assert "虚构" in audit_system
    assert "供应商限制" in audit_system
    assert "不得改写" in audit_system
    assert json.dumps(
        AuditReport.model_json_schema(),
        ensure_ascii=False,
        separators=(",", ":"),
    ) in model.requests[1][1]["content"]
