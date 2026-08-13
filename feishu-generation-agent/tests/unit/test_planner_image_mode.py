import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from feishu_generation_agent.domain.document import (
    DocumentBlock,
    MediaAsset,
    NormalizedDocument,
    SourceType,
    VisionDescription,
)
from feishu_generation_agent.integrations.planner import (
    DeepSeekPlanner,
    image_planner_system_prompt,
    planner_system_prompt,
    validate_plan,
)


class FakePlanModel:
    """最小可用的 DeepSeek 替身，只记录请求并按序返回响应。"""

    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.requests: list[list[dict[str, Any]]] = []

    def bind(self, **_kwargs: Any) -> "FakePlanModel":
        return self

    async def ainvoke(
        self,
        messages: list[dict[str, Any]],
        config: dict[str, Any] | None = None,
    ) -> object:
        self.requests.append(copy.deepcopy(messages))
        return SimpleNamespace(
            content=self.responses.pop(0),
            additional_kwargs={},
        )


def _cg_document(tmp_path: Path) -> NormalizedDocument:
    path = tmp_path / "sarah.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n")
    return NormalizedDocument(
        document_id="doc-cg",
        title="【剧】女儿穿越救母_day6_CG图需求",
        revision=85,
        source_type=SourceType.WIKI,
        source_token="token-cg",
        blocks=[
            DocumentBlock(
                block_id="cg-1",
                parent_id=None,
                block_type="text",
                order=0,
                path=["cg-1"],
                text="编号 1：Victor 中景，脸部因为愤怒而变得扭曲",
                image_asset_id=None,
            ),
            DocumentBlock(
                block_id="cg-image",
                parent_id=None,
                block_type="image",
                order=1,
                path=["cg-image"],
                text="",
                image_asset_id="image-1",
            ),
        ],
        text_view=(
            "[block:cg-1] 编号 1：Victor 中景，脸部因为愤怒而变得扭曲\n"
            "[image:image-1]"
        ),
        media_assets=[
            MediaAsset(
                asset_id="image-1",
                source_block_id="cg-image",
                origin="document",
                local_path=path,
                mime_type="image/png",
                size=8,
                sha256="a" * 64,
            )
        ],
    )


def _visions() -> list[VisionDescription]:
    return [
        VisionDescription(
            asset_id="image-1",
            subjects=["金色长发女性角色"],
            scene="角色设定图",
            style="3D 卡通迪士尼",
            composition="正面全身",
            characters=["Sarah"],
            actions=["站立"],
            visible_text=[],
            colors=["金色", "蓝色"],
            probable_role="reference_image",
            uncertainties=[],
        )
    ]


def _image_plan_json(**updates: object) -> str:
    task: dict[str, Any] = {
        "task_id": "task-cg-1",
        "task_type": "image_to_image",
        "title": "Victor 中景",
        "source_block_ids": ["cg-1"],
        "user_intent": "生成 Victor 愤怒表情的 CG 插图",
        "prompt": (
            "@图片1 中的男性角色中景，脸部因愤怒而扭曲，"
            "戏剧化顶光 + 侧逆光，3D 卡通迪士尼风格"
        ),
        "reference_images": [
            {"asset_id": "image-1", "role": "reference_image", "order": 1}
        ],
        "aspect_ratio": "9:16",
        "image_size": "2K",
        "output_count": 1,
        "confidence": 0.9,
    }
    task.update(updates)
    return json.dumps(
        {"tasks": [task], "document_summary": "CG 插图需求"},
        ensure_ascii=False,
    )


def test_video_planner_prompt_hash_is_still_frozen():
    """图片模式不得改动视频 system prompt。"""
    assert hashlib.sha256(
        planner_system_prompt().encode("utf-8")
    ).hexdigest() == (
        "fc009b4bb8351502a9412b88a5554a8567a9aa9a633eba588fb673b513f16db1"
    )


def test_image_planner_prompt_is_distinct_and_image_specific():
    image_prompt = image_planner_system_prompt()
    assert image_prompt != planner_system_prompt()
    # 图片契约要讲的
    assert "image_to_image" in image_prompt
    assert "光" in image_prompt
    assert "size_variants" in image_prompt
    assert "image_provider" in image_prompt
    # 视频契约的东西不该出现
    assert "运镜" not in image_prompt
    assert "first_last_frame" not in image_prompt
    assert "generate_audio" not in image_prompt


def test_image_contract_explains_every_required_field():
    """契约必须交代所有必填字段，否则模型漏填导致三次重试全废。

    真实文档跑失败过一次：契约只讲了 size_variants 没讲 image_size，
    模型漏填该必填字段，三次结构化输出全部校验不通过。
    """
    image_prompt = image_planner_system_prompt()
    assert "image_size" in image_prompt
    for token in ("1K", "1.5K", "2K"):
        assert token in image_prompt, f"缺少 image_size 取值说明：{token}"


async def test_plan_mode_image_uses_image_system_prompt(tmp_path: Path):
    document = _cg_document(tmp_path)
    model = FakePlanModel([_image_plan_json()])

    await DeepSeekPlanner(model).plan(document, _visions(), mode="image")

    assert model.requests[0][0]["content"] == image_planner_system_prompt()


async def test_plan_mode_defaults_to_video(tmp_path: Path):
    """不传 mode 时行为与改动前完全一致。"""
    document = _cg_document(tmp_path)
    model = FakePlanModel([_image_plan_json()])

    await DeepSeekPlanner(model).plan(document, _visions())

    assert model.requests[0][0]["content"] == planner_system_prompt()


async def test_image_mode_portal_prompt_composes_with_image_contract(
    tmp_path: Path,
):
    document = _cg_document(tmp_path)
    model = FakePlanModel([_image_plan_json()])
    portal_prompt = "业务偏好：角色一致性优先。"

    await DeepSeekPlanner(model).plan(
        document, _visions(), system_prompt=portal_prompt, mode="image"
    )

    composed = model.requests[0][0]["content"]
    assert "不可编辑" in composed
    assert composed.endswith(portal_prompt)
    assert "size_variants" in composed
    assert "运镜" not in composed


def test_validate_plan_image_contract_is_off_by_default(tmp_path: Path):
    """默认关闭，存量图片任务（无 @图片N、无光影）不受影响。"""
    document = _cg_document(tmp_path)
    payload = json.loads(_image_plan_json(prompt="一张竖版海报"))

    issues = validate_plan(payload, document, 4)

    assert not any("光影" in issue for issue in issues)


def test_validate_plan_image_contract_flags_missing_lighting(tmp_path: Path):
    document = _cg_document(tmp_path)
    payload = json.loads(
        _image_plan_json(prompt="@图片1 中的男性角色中景，脸部因愤怒而扭曲")
    )

    issues = validate_plan(
        payload, document, 4, enforce_image_prompt_contract=True
    )

    assert any("光影" in issue for issue in issues)


def test_validate_plan_image_contract_flags_video_vocabulary(tmp_path: Path):
    document = _cg_document(tmp_path)
    payload = json.loads(
        _image_plan_json(
            prompt=(
                "@图片1 中的男性角色中景，脸部因愤怒扭曲，"
                "戏剧化顶光，镜头运动缓慢推进"
            )
        )
    )

    issues = validate_plan(
        payload, document, 4, enforce_image_prompt_contract=True
    )

    assert any("镜头运动" in issue for issue in issues)


def test_validate_plan_image_contract_accepts_valid_prompt(tmp_path: Path):
    document = _cg_document(tmp_path)
    payload = json.loads(_image_plan_json())

    issues = validate_plan(
        payload, document, 4, enforce_image_prompt_contract=True
    )

    assert issues == []


def test_validate_plan_image_contract_ignores_video_tasks(tmp_path: Path):
    """开启图片契约时，视频任务仍走视频契约，不被图片规则误伤。"""
    document = _cg_document(tmp_path)
    payload = json.loads(
        _image_plan_json(
            task_type="image_to_video",
            image_size=None,
            duration=5,
            resolution="720p",
            prompt="@图片1 中的男性角色缓慢转头，运镜跟随",
        )
    )

    issues = validate_plan(
        payload, document, 4, enforce_image_prompt_contract=True
    )

    assert not any("禁止视频语汇" in issue for issue in issues)
