import copy
import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from langchain_core.runnables.config import ensure_config, set_config_context
from langsmith import tracing_context
from langsmith.utils import tracing_is_enabled

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
    planner_system_prompt,
    validate_plan,
)


def test_planner_system_prompt_prime_hash_is_frozen() -> None:
    prime = planner_system_prompt()

    assert hashlib.sha256(prime.encode("utf-8")).hexdigest() == (
        "fc009b4bb8351502a9412b88a5554a8567a9aa9a633eba588fb673b513f16db1"
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
    sources = source_block_ids or ["story-1"]
    storyboard_sources = [
        block_id for block_id in sources if block_id.startswith("shot-")
    ]
    if len(storyboard_sources) >= 2:
        prompt = "\n".join(
            [
                "参考 @图片1 中的蓝色纸船。",
                *[
                    f"镜头 {index}：固定镜头展示 @图片1 中的蓝色纸船。"
                    for index, _ in enumerate(storyboard_sources, start=1)
                ],
                (
                    "高清，纸船稳定不变形、运动连贯，"
                    "不要生成水印，不要生成 Logo。"
                ),
            ]
        )
    else:
        prompt = (
            "参考 @图片1 中的蓝色纸船，生成纸船从近景漂向远处的画面。"
        )
    return {
        "task_id": task_id,
        "task_type": "image_to_video",
        "title": "纸船漂流短片",
        "source_block_ids": sources,
        "user_intent": "生成连续的纸船漂流视频",
        "prompt": prompt,
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
        self.configs: list[dict[str, Any]] = []
        self.tracing_enabled: list[bool | str] = []
        self.api_key = "fictional-deepseek-key-must-not-leak"

    def bind(self, **kwargs: Any) -> "FakeDeepSeekModel":
        self.bind_calls.append(kwargs)
        return self

    async def ainvoke(
        self,
        messages: list[dict[str, Any]],
        config: dict[str, Any] | None = None,
    ) -> object:
        self.calls += 1
        self.requests.append(copy.deepcopy(messages))
        resolved_config = ensure_config(config)
        self.configs.append(resolved_config)
        self.tracing_enabled.append(tracing_is_enabled())
        callbacks = resolved_config.get("callbacks")
        if isinstance(callbacks, list):
            for callback in callbacks:
                recorder = getattr(callback, "record_model_input", None)
                if recorder is not None:
                    recorder(messages)
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
    assert "可用素材引用=" in user_prompt
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
    assert "每个下载成功的素材" in user_prompt
    assert "excluded_assets" in user_prompt
    assert "保留蓝色" in user_prompt
    combined_contract = request[0]["content"] + "\n" + user_prompt
    for required in (
        "@图片N",
        "逐张读取",
        "每个镜头",
        "不得机械平均分配",
        "镜头 1",
        "禁止绝对秒数",
        "画质",
        "水印",
        "Logo",
    ):
        assert required in combined_contract


async def test_planner_repairs_hotpot_reference_bindings(
    storyboard_document: NormalizedDocument,
    vision_descriptions: list[VisionDescription],
) -> None:
    sources = [f"shot-{index}" for index in range(1, 5)]
    invalid = _video_task(source_block_ids=sources)
    invalid["prompt"] = (
        "0-3秒：展示空锅。3-8秒：食材入锅。"
        "8-12秒：俯拍成品。"
    )
    repaired = _video_task(source_block_ids=sources)
    model = FakeDeepSeekModel(
        [_plan_json(invalid), _plan_json(repaired)]
    )

    plan = await DeepSeekPlanner(model).plan(
        storyboard_document,
        vision_descriptions,
    )

    assert model.calls == 2
    repair_prompt = model.requests[1][-1]["content"]
    assert "@图片1" in repair_prompt
    assert "镜头 1" in repair_prompt
    assert "绝对秒数" in repair_prompt
    assert plan.tasks[0].prompt == repaired["prompt"]


async def test_planner_removes_video_only_image_size_without_model_retry(
    narrative_document: NormalizedDocument,
    vision_descriptions: list[VisionDescription],
) -> None:
    task = _video_task()
    task["image_size"] = "4K"
    task["prompt"] = (
        "使用 @图片1 作为蓝色纸船的造型参考，"
        "生成纸船沿河漂流的连续画面。"
    )
    model = FakeDeepSeekModel([_plan_json(task)])

    plan = await DeepSeekPlanner(model).plan(
        narrative_document,
        vision_descriptions,
    )

    assert model.calls == 1
    assert plan.tasks[0].image_size is None


async def test_default_and_portal_planner_system_prompts_are_composed_safely(
    narrative_document: NormalizedDocument,
    vision_descriptions: list[VisionDescription],
):
    portal_prompt = "个人业务偏好：优先保持角色一致。"
    default_model = FakeDeepSeekModel([_plan_json(_video_task())])
    portal_model = FakeDeepSeekModel([_plan_json(_video_task())])

    await DeepSeekPlanner(default_model).plan(
        narrative_document, vision_descriptions
    )
    await DeepSeekPlanner(portal_model).plan(
        narrative_document,
        vision_descriptions,
        system_prompt=portal_prompt,
    )

    assert default_model.requests[0][0]["content"] == planner_system_prompt()
    composed = portal_model.requests[0][0]["content"]
    assert composed != planner_system_prompt()
    assert "不可编辑" in composed
    assert "冲突" in composed
    assert composed.index("不可编辑") < composed.index(portal_prompt)
    assert composed.endswith(portal_prompt)


async def test_exact_system_prompt_override_replays_checkpoint_verbatim(
    narrative_document: NormalizedDocument,
    vision_descriptions: list[VisionDescription],
):
    historical_prime = "历史 Prime 快照：必须逐字复用"
    model = FakeDeepSeekModel([_plan_json(_video_task())])

    await DeepSeekPlanner(model).plan(
        narrative_document,
        vision_descriptions,
        exact_system_prompt=historical_prime,
    )

    assert model.requests[0][0]["content"] == historical_prime


async def test_portal_prompt_is_not_exposed_in_planner_error_or_logs(
    narrative_document: NormalizedDocument,
    vision_descriptions: list[VisionDescription],
    caplog: pytest.LogCaptureFixture,
):
    secret_prompt = "绝不允许泄漏的个人完整提示词标记"
    model = FakeDeepSeekModel(
        [
            RateLimitFailure("fictional-secret-rate-limit"),
        ]
    )

    with pytest.raises(AgentError) as raised:
        await DeepSeekPlanner(model).plan(
            narrative_document,
            vision_descriptions,
            system_prompt=secret_prompt,
        )

    serialized = json.dumps(
        raised.value.detail.model_dump(mode="json"), ensure_ascii=False
    )
    assert secret_prompt not in serialized
    assert secret_prompt not in caplog.text


async def test_planner_and_audit_inputs_do_not_enter_inherited_tracing_callbacks(
    narrative_document: NormalizedDocument,
    vision_descriptions: list[VisionDescription],
):
    secret_prompt = "私有业务系统提示词：禁止进入任何追踪记录"
    audit_json = json.dumps(
        {"issues": [], "corrections_required": False},
        ensure_ascii=False,
    )
    model = FakeDeepSeekModel([_plan_json(_video_task()), audit_json])
    planner = DeepSeekPlanner(model)

    class _Recorder:
        def __init__(self) -> None:
            self.inputs: list[list[dict[str, Any]]] = []

        def record_model_input(self, messages: list[dict[str, Any]]) -> None:
            self.inputs.append(copy.deepcopy(messages))

    recorder = _Recorder()
    with tracing_context(enabled=True):
        with set_config_context({"callbacks": [recorder]}) as context:
            plan_task = context.run(
                asyncio.create_task,
                planner.plan(
                    narrative_document,
                    vision_descriptions,
                    system_prompt=secret_prompt,
                ),
            )
            plan = await plan_task
            audit_task = context.run(
                asyncio.create_task,
                planner.audit(narrative_document, plan),
            )
            await audit_task

    assert recorder.inputs == []
    assert model.tracing_enabled == [False, False]
    assert [config.get("callbacks") for config in model.configs] == [[], []]


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
    model = FakeDeepSeekModel(
        ["not-json-first", "not-json-second", "not-json-third"]
    )
    planner = DeepSeekPlanner(model, max_output_count=4)

    with pytest.raises(AgentError) as raised:
        await planner.plan(narrative_document, vision_descriptions)

    assert model.calls == 3
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
    third = json.loads(_plan_json(_video_task()))
    third["tasks"][0]["task_type"] = None
    model = FakeDeepSeekModel(
        [
            json.dumps(first, ensure_ascii=False),
            json.dumps(second, ensure_ascii=False),
            json.dumps(third, ensure_ascii=False),
        ]
    )
    planner = DeepSeekPlanner(model, max_output_count=4)

    with pytest.raises(AgentError) as raised:
        await planner.plan(narrative_document, vision_descriptions)

    assert model.calls == 3
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
    model = FakeDeepSeekModel([_plan_json(), _plan_json(), _plan_json()])
    planner = DeepSeekPlanner(model, max_output_count=4)

    with pytest.raises(AgentError) as raised:
        await planner.plan(narrative_document, vision_descriptions)

    assert model.calls == 3
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


def test_validator_requires_exact_successful_asset_coverage(
    narrative_document: NormalizedDocument,
    tmp_path: Path,
):
    assets = [
        _asset(tmp_path, f"asset-{index}", f"image-{index}")
        for index in range(1, 4)
    ]
    document = narrative_document.model_copy(
        update={"media_assets": assets}
    )
    task = _video_task()
    task["reference_images"] = [
        {"asset_id": "asset-1", "role": "reference_image", "order": 1},
        {"asset_id": "asset-2", "role": "reference_image", "order": 2},
    ]
    task["prompt"] = (
        "参考 @图片1 中的蓝色纸船和 @图片2 中的绿色河岸，"
        "生成连续漂流画面。"
    )
    valid = {
        "tasks": [task],
        "document_summary": "测试生成需求",
        "excluded_assets": [
            {
                "asset_id": "asset-3",
                "reason": "供应商最多支持两张参考图，保留主体与场景图。",
            }
        ],
    }

    assert validate_plan(valid, document, 4) == []

    uncovered = copy.deepcopy(valid)
    uncovered["excluded_assets"] = []
    issues = validate_plan(uncovered, document, 4)
    assert any("asset-3" in issue and "uncovered" in issue for issue in issues)

    overlap = copy.deepcopy(valid)
    overlap["excluded_assets"][0]["asset_id"] = "asset-2"
    issues = validate_plan(overlap, document, 4)
    assert any("excluded_assets" in issue and "asset-2" in issue for issue in issues)

    english_reason = copy.deepcopy(valid)
    english_reason["excluded_assets"][0]["reason"] = "provider supports two"
    issues = validate_plan(english_reason, document, 4)
    assert any(
        "excluded_assets[0].reason" in issue and "中文" in issue
        for issue in issues
    )


def test_validator_rejects_missing_failed_and_duplicate_asset_assignments(
    narrative_document: NormalizedDocument,
    tmp_path: Path,
):
    failed = _asset(
        tmp_path,
        "asset-failed",
        "image-failed",
        download_error="fictional download failure",
    )
    document = narrative_document.model_copy(
        update={"media_assets": [*narrative_document.media_assets, failed]}
    )
    task = _video_task()
    task["reference_images"] = [
        {"asset_id": "missing", "role": "reference_image", "order": 1},
        {"asset_id": "asset-failed", "role": "reference_image", "order": 2},
    ]
    raw = {
        "tasks": [task],
        "document_summary": "测试生成需求",
        "excluded_assets": [
            {"asset_id": "asset-1", "reason": "用户选择了其他主体图"},
            {"asset_id": "asset-1", "reason": "供应商数量限制"},
        ],
    }

    issues = validate_plan(raw, document, 4)

    assert any("unknown asset_id missing" in issue for issue in issues)
    assert any("asset-failed" in issue and "download failed" in issue for issue in issues)
    assert any("duplicate" in issue and "asset-1" in issue for issue in issues)


def test_validator_keeps_existing_multimodal_reference_support(
    narrative_document: NormalizedDocument,
    tmp_path: Path,
):
    video = _asset(
        tmp_path,
        "video-1",
        "video-block",
        mime_type="video/mp4",
    )
    audio = _asset(
        tmp_path,
        "audio-1",
        "audio-block",
        mime_type="audio/mpeg",
    )
    document = narrative_document.model_copy(
        update={
            "media_assets": [
                *narrative_document.media_assets,
                video,
                audio,
            ]
        }
    )
    task = _video_task()
    task["reference_images"] = [
        {"asset_id": "asset-1", "role": "reference_image", "order": 1},
        {"asset_id": "video-1", "role": "reference_video", "order": 2},
        {"asset_id": "audio-1", "role": "reference_audio", "order": 3},
    ]
    task["reference_mode"] = "multi_reference"
    task["generate_audio"] = True
    task["prompt"] = (
        "参考 @图片1 中的蓝色纸船主体，"
        "参考 @视频1 中的平稳跟拍运镜，"
        "参考 @音频1 中的轻柔流水声音，生成连续漂流画面。"
    )
    raw = {
        "tasks": [task],
        "document_summary": "测试多模态生成需求",
        "excluded_assets": [],
    }

    assert validate_plan(raw, document, 4) == []


@pytest.mark.parametrize(
    ("field_path", "value", "expected_issue"),
    [
        ("document_summary", "Generate a paper boat video", "document_summary"),
        ("user_intent", "Generate a paper boat video", "tasks[0].user_intent"),
        ("prompt", "A paper boat drifts down a river", "tasks[0].prompt"),
    ],
)
def test_validator_rejects_english_only_required_planning_fields(
    narrative_document: NormalizedDocument,
    field_path: str,
    value: str,
    expected_issue: str,
):
    raw_plan = json.loads(_plan_json(_video_task()))
    if field_path == "document_summary":
        raw_plan[field_path] = value
    else:
        raw_plan["tasks"][0][field_path] = value

    issues = validate_plan(raw_plan, narrative_document, 4)

    assert any(expected_issue in issue and "中文" in issue for issue in issues)


@pytest.mark.parametrize("summary", [None, "", "English summary only"])
def test_validator_requires_cjk_document_summary_for_raw_json(
    narrative_document: NormalizedDocument,
    summary: str | None,
):
    raw_plan = json.loads(_plan_json(_video_task()))
    if summary is None:
        raw_plan.pop("document_summary")
    else:
        raw_plan["document_summary"] = summary

    issues = validate_plan(raw_plan, narrative_document, 4)

    assert "plan.document_summary: 必须包含中文主体说明" in issues


def test_validator_requires_cjk_document_summary_for_task_plan(
    narrative_document: NormalizedDocument,
):
    typed_plan = TaskPlan.model_validate(
        {
            "tasks": [_video_task()],
            "document_summary": "English summary only",
        }
    )

    issues = validate_plan(typed_plan, narrative_document, 4)

    assert "plan.document_summary: 必须包含中文主体说明" in issues


def test_validator_accepts_chinese_prompt_with_requested_english_dialogue_brand_and_ui(
    narrative_document: NormalizedDocument,
):
    raw_plan = json.loads(_plan_json(_video_task()))
    raw_plan["tasks"][0]["prompt"] = (
        "参考 @图片1 中的蓝色纸船；近景镜头中，角色说："
        "\"Don't move.\"；画面保留 Coca-Cola 品牌，"
        "并在 UI 上显示 Start 按钮。"
    )
    raw_plan["tasks"][0]["generate_audio"] = True

    assert validate_plan(raw_plan, narrative_document, 4) == []


@pytest.mark.parametrize("generate_audio", [None, False])
def test_validator_requires_audio_for_video_when_document_requests_it(
    narrative_document: NormalizedDocument,
    generate_audio: bool | None,
):
    blocks = [
        block.model_copy(
            update={
                "text": (
                    "女孩说：\"Wait, what's this?\"；"
                    "音效：清脆提示音；Upbeat electronic BGM starts。"
                )
            }
        )
        if block.block_id == "story-1"
        else block
        for block in narrative_document.blocks
    ]
    document = narrative_document.model_copy(
        update={
            "blocks": blocks,
            "text_view": (
                "[block:story-1] 女孩说：\"Wait, what's this?\"；"
                "音效：清脆提示音；Upbeat electronic BGM starts。\n"
                "[block:image-1] [image:asset-1]"
            ),
        }
    )
    task = _video_task()
    task["generate_audio"] = generate_audio

    issues = validate_plan(
        json.loads(_plan_json(task)),
        document,
        4,
    )

    assert (
        "tasks[0].generate_audio: "
        "文档明确要求对白、音效、配音、环境音或音乐时必须为 true"
    ) in issues


def test_validator_scopes_audio_intent_to_each_video_task(
    narrative_document: NormalizedDocument,
    tmp_path: Path,
):
    second_asset = _asset(tmp_path, "asset-2", "story-2")
    blocks = [
        block.model_copy(
            update={"text": "女孩说：\"开始吧。\"；音效：清脆提示音。"}
        )
        if block.block_id == "story-1"
        else block
        for block in narrative_document.blocks
    ]
    blocks.append(
        DocumentBlock(
            block_id="story-2",
            parent_id="page-1",
            block_type="text",
            order=3,
            path=["page-1", "story-2"],
            text="第二条视频只展示纸船漂流，视频保持静音。",
        )
    )
    document = narrative_document.model_copy(
        update={
            "blocks": blocks,
            "text_view": (
                "[block:story-1] 女孩说：\"开始吧。\"；音效：清脆提示音。\n"
                "[block:story-2] 第二条视频只展示纸船漂流，视频保持静音。\n"
                "[block:image-1] [image:asset-1]"
            ),
            "media_assets": [*narrative_document.media_assets, second_asset],
        }
    )
    audio_task = _video_task()
    audio_task["generate_audio"] = True
    silent_task = _video_task(
        "task-silent",
        source_block_ids=["story-2"],
        asset_id="asset-2",
    )
    silent_task["generate_audio"] = False

    assert validate_plan(
        json.loads(_plan_json(audio_task, silent_task)),
        document,
        4,
    ) == []


@pytest.mark.parametrize(
    "requirement",
    [
        "女孩说：\"Wait, what's this?\"",
        "对白：保持安静。",
        "台词旁白：Remove all arrows.",
        "音效：清脆提示音。",
        "Upbeat electronic BGM starts.",
    ],
)
def test_validator_recognizes_positive_audio_intent_without_global_keywords(
    narrative_document: NormalizedDocument,
    requirement: str,
):
    blocks = [
        block.model_copy(update={"text": requirement})
        if block.block_id == "story-1"
        else block
        for block in narrative_document.blocks
    ]
    document = narrative_document.model_copy(
        update={
            "blocks": blocks,
            "text_view": (
                f"[block:story-1] {requirement}\n"
                "[block:image-1] [image:asset-1]"
            ),
        }
    )
    task = _video_task()
    task["generate_audio"] = None

    issues = validate_plan(
        json.loads(_plan_json(task)),
        document,
        4,
    )

    assert any("tasks[0].generate_audio" in issue for issue in issues)


@pytest.mark.parametrize(
    "requirement",
    [
        "视频保持静音，无需配音，不要音效，无背景音乐。",
        "不要有配音。",
        "背景音乐：无。",
        "无声视频，但画面出现音效按钮。",
    ],
)
def test_validator_does_not_treat_explicit_silence_or_ui_text_as_audio_intent(
    narrative_document: NormalizedDocument,
    requirement: str,
):
    blocks = [
        block.model_copy(update={"text": requirement})
        if block.block_id == "story-1"
        else block
        for block in narrative_document.blocks
    ]
    document = narrative_document.model_copy(
        update={
            "blocks": blocks,
            "text_view": (
                f"[block:story-1] {requirement}\n"
                "[block:image-1] [image:asset-1]"
            ),
        }
    )
    task = _video_task()
    task["generate_audio"] = False

    assert validate_plan(
        json.loads(_plan_json(task)),
        document,
        4,
    ) == []


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


def test_validator_rejects_hotpot_storyboard_without_understood_references(
    storyboard_document: NormalizedDocument,
):
    task = _video_task(
        source_block_ids=[f"shot-{index}" for index in range(1, 5)]
    )
    task["prompt"] = (
        "0-3秒：展示空锅。3-8秒：食材入锅。"
        "8-12秒：俯拍成品。"
    )

    issues = validate_plan(
        json.loads(_plan_json(task)),
        storyboard_document,
        4,
        enforce_seedance_prompt_contract=True,
    )

    assert any("@图片1" in issue for issue in issues)
    assert any("镜头 1" in issue for issue in issues)
    assert any("绝对秒数" in issue for issue in issues)


def test_validator_rejects_noncontinuous_reference_order(
    narrative_document: NormalizedDocument,
    tmp_path: Path,
):
    second = _asset(tmp_path, "asset-2", "image-2")
    document = narrative_document.model_copy(
        update={
            "media_assets": [
                *narrative_document.media_assets,
                second,
            ]
        }
    )
    task = _video_task()
    task["reference_images"].append(
        {"asset_id": "asset-2", "role": "reference_image", "order": 3}
    )
    task["prompt"] = (
        "参考 @图片1 中的蓝色纸船和 @图片2 中的绿色河岸，"
        "生成连续漂流画面。"
    )

    issues = validate_plan(
        json.loads(_plan_json(task)),
        document,
        4,
        enforce_seedance_prompt_contract=True,
    )

    assert any("1…N" in issue for issue in issues)


def test_validator_keeps_legacy_seedance_prompt_compatible_by_default(
    storyboard_document: NormalizedDocument,
):
    task = _video_task(
        source_block_ids=[f"shot-{index}" for index in range(1, 5)]
    )
    task["prompt"] = (
        "0-3秒：展示空锅。3-8秒：食材入锅。"
        "8-12秒：俯拍成品。"
    )

    assert validate_plan(
        json.loads(_plan_json(task)), storyboard_document, 4
    ) == []


def test_strict_validator_enforces_shot_bindings_for_narrative_multishot(
    narrative_document: NormalizedDocument,
):
    task = _video_task()
    task["user_intent"] = "生成一个包含多个镜头的纸船短片"
    task["prompt"] = (
        "参考 @图片1 中的蓝色纸船。\n"
        "镜头 1：展示纸船。\n"
        "镜头 2：纸船驶向远方。\n"
        "画面稳定不变形，无水印，无 Logo。"
    )

    issues = validate_plan(
        json.loads(_plan_json(task)),
        narrative_document,
        4,
        enforce_seedance_prompt_contract=True,
    )

    assert any("镜头 1" in issue and "素材" in issue for issue in issues)
    assert any("镜头 2" in issue and "素材" in issue for issue in issues)


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


async def test_audit_repairs_goal_rejecting_supplier_limit_language(
    narrative_document: NormalizedDocument,
    vision_descriptions: list[VisionDescription],
):
    plan_json = _plan_json(_video_task())
    rejecting_audit = json.dumps(
        {
            "issues": [
                "供应商无法保证尾帧一致。",
                "多参考接口做不到使用全部参考图。",
            ],
            "corrections_required": True,
        },
        ensure_ascii=False,
    )
    forged_empty_repair = json.dumps(
        {
            "issues": [],
            "corrections_required": False,
        },
        ensure_ascii=False,
    )
    model = FakeDeepSeekModel(
        [plan_json, rejecting_audit, forged_empty_repair]
    )
    planner = DeepSeekPlanner(model, max_output_count=4)
    plan = await planner.plan(narrative_document, vision_descriptions)

    report = await planner.audit(narrative_document, plan)

    assert len(report.issues) == 2
    assert "尾帧一致" in report.issues[0]
    assert "全部参考图" in report.issues[1]
    assert report.corrections_required is True
    assert not any(
        term in issue
        for issue in report.issues
        for term in ("无法保证", "做不到")
    )
    assert all(
        issue.startswith(("实施策略：", "风险缓释："))
        for issue in report.issues
    )
    assert model.calls == 2
    audit_system = model.requests[1][0]["content"]
    assert "不要否定或质疑需求目标" in audit_system
    assert "实施策略" in audit_system


async def test_audit_technical_blocker_requires_actionable_human_handling(
    narrative_document: NormalizedDocument,
    vision_descriptions: list[VisionDescription],
):
    plan_json = _plan_json(_video_task())
    audit_json = json.dumps(
        {
            "issues": ["技术阻断：供应商不支持该素材格式。"],
            "corrections_required": True,
        },
        ensure_ascii=False,
    )
    model = FakeDeepSeekModel([plan_json, audit_json])
    planner = DeepSeekPlanner(model, max_output_count=4)
    plan = await planner.plan(narrative_document, vision_descriptions)

    report = await planner.audit(narrative_document, plan)

    assert len(report.issues) == 1
    assert report.issues[0].startswith("技术阻断：")
    assert "该素材格式" in report.issues[0]
    assert "人工处理" in report.issues[0]
    assert report.corrections_required is True
