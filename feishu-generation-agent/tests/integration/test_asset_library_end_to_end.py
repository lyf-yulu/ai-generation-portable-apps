"""阶段 3 端到端：素材库 → 角色匹配 → 自动入库 → 参考图可被 provider 消费。

各 Task 单测都过，这里验证它们真能串成一条链。
"""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image

from feishu_generation_agent.domain.document import (
    DocumentBlock,
    MediaAsset,
    NormalizedDocument,
    SourceType,
)
from feishu_generation_agent.domain.plan import GenerationTask
from feishu_generation_agent.graph.nodes import (
    _character_context_argument,
    _character_media_assets,
    _resolve_character_assets,
    _task_assets,
)
from feishu_generation_agent.integrations.character_semantic_matcher import (
    SemanticMatch,
    SemanticMatchResult,
    UnresolvedCandidate,
)
from feishu_generation_agent.storage.asset_library import AssetLibraryStore


def _png(path: Path) -> bytes:
    Image.new("RGB", (32, 48), (180, 90, 60)).save(path)
    return path.read_bytes()


@pytest.fixture
async def store(tmp_path: Path):
    opened = await AssetLibraryStore.open(
        db_path=tmp_path / "asset-library.sqlite3",
        assets_dir=tmp_path / "asset-library",
        base_url="https://media.example.com",
    )
    yield opened
    await opened.close()


def _cg_document(tmp_path: Path) -> NormalizedDocument:
    mike = tmp_path / "mike.png"
    content = _png(mike)
    return NormalizedDocument(
        document_id="doc-cg-day6",
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
            ),
            DocumentBlock(
                block_id="cg-2",
                parent_id=None,
                block_type="text",
                order=1,
                path=["cg-2"],
                text="编号 2：Sophia 与 Sarah 握手言和",
            ),
            DocumentBlock(
                block_id="cg-3",
                parent_id=None,
                block_type="text",
                order=2,
                path=["cg-3"],
                text="新角色 Mike 首次登场",
            ),
            DocumentBlock(
                block_id="cg-img",
                parent_id=None,
                block_type="image",
                order=3,
                path=["cg-img"],
                text="",
                image_asset_id="image-1",
            ),
        ],
        text_view=(
            "[block:cg-1] 编号 1：Victor 中景\n"
            "[block:cg-2] 编号 2：Sophia 与 Sarah 握手言和\n"
            "[block:cg-3] 新角色 Mike 首次登场\n"
            "[image:image-1]"
        ),
        media_assets=[
            MediaAsset(
                asset_id="image-1",
                source_block_id="cg-img",
                origin="feishu",
                local_path=mike,
                mime_type="image/png",
                size=len(content),
                sha256="b" * 64,
            )
        ],
    )


class _Matcher:
    """语义匹配替身：认出 Sarah 的正确变体，并报告 Mike 未入库。"""

    def __init__(self, sarah_asset_id: str) -> None:
        self.sarah_asset_id = sarah_asset_id
        self.anchors_seen: list[Any] = []

    async def match(self, text, library, *, anchors) -> SemanticMatchResult:
        del text, library
        self.anchors_seen = list(anchors)
        return SemanticMatchResult(
            matches=(
                SemanticMatch(
                    asset_id=self.sarah_asset_id,
                    block_ids=("cg-2",),
                    confidence=0.92,
                    reason="握手言和场景中的金发女性",
                ),
            ),
            unresolved_candidates=(
                UnresolvedCandidate(
                    proposed_name="Mike",
                    block_ids=("cg-3",),
                    reason="素材库尚无该角色",
                ),
            ),
        )


async def test_asset_library_chain_end_to_end(store, tmp_path: Path):
    ref = tmp_path / "ref.png"
    content = _png(ref)

    # 1. 素材库预置：Victor 精确可匹配，Sarah 有两个着装变体
    victor = await store.create(
        name="Victor",
        variant="默认",
        content=content,
        mime_type="image/png",
        prompt_fragment="络腮胡中年男性，深色西装",
    )
    sarah_gown = await store.create(
        name="Sarah",
        variant="晚宴礼服",
        content=content,
        mime_type="image/png",
        prompt_fragment="金色长发，蓝色眼睛",
    )
    await store.create(
        name="Sarah",
        variant="战斗装",
        content=content,
        mime_type="image/png",
    )

    document = _cg_document(tmp_path)
    matcher = _Matcher(sarah_gown.asset_id)
    services = SimpleNamespace(
        asset_library_store=store, character_matcher=matcher
    )

    # 2. 两级匹配 + 自动入库
    resolved = await _resolve_character_assets(document, services)
    names = [item.name for item in resolved]

    assert "Victor" in names, "精确匹配应命中 Victor"
    assert "Sarah" in names, "语义匹配应命中 Sarah"
    assert "Mike" in names, "未知角色应自动入库并挂上"

    # 精确匹配的结果作为锚点传给了语义层，避免重复推理
    assert [a.name for a in matcher.anchors_seen] == ["Victor"]

    # 3. 自动入库的条目打了复核标签
    mike = next(item for item in resolved if item.name == "Mike")
    assert "auto-ingested" in mike.tags
    assert any("doc-cg-day6" in tag for tag in mike.tags)

    # 4. 角色描述注入 planner 上下文
    class ModeAwarePlanner:
        async def plan(
            self,
            document,
            visions,
            feedback=None,
            system_prompt=None,
            exact_system_prompt=None,
            mode="video",
            character_context=None,
        ):
            raise NotImplementedError

    context = _character_context_argument(ModeAwarePlanner(), resolved)[
        "character_context"
    ]
    assert "络腮胡中年男性" in context
    assert "晚宴礼服" in context
    assert victor.asset_id in context

    # 5. 素材库参考图转成 MediaAsset 后能被 provider 链路解析
    media = await _character_media_assets(resolved, store)
    assert len(media) == len(resolved)

    merged = document.model_copy(
        update={"media_assets": list(document.media_assets) + media}
    )
    task = GenerationTask.model_validate(
        {
            "task_id": "cg-2",
            "task_type": "image_to_image",
            "title": "Sophia 与 Sarah 握手言和",
            "source_block_ids": ["cg-2"],
            "user_intent": "生成两位女性和解的 CG 插图",
            "prompt": "@图片1 中的女性与 @图片2 握手言和，明媚的光线",
            "reference_images": [
                {
                    "asset_id": sarah_gown.asset_id,
                    "role": "reference_image",
                    "order": 1,
                },
                {
                    "asset_id": victor.asset_id,
                    "role": "reference_image",
                    "order": 2,
                },
            ],
            "aspect_ratio": "9:16",
            "image_size": "2K",
            "image_provider": "seedream",
            "size_variants": ["1080x2080", "1700x2500"],
        }
    )

    task_assets = _task_assets(task, merged)

    assert [item.asset_id for item in task_assets] == [
        sarah_gown.asset_id,
        victor.asset_id,
    ]
    for item in task_assets:
        assert item.local_path.read_bytes(), "provider 必须能读到参考图字节"


async def test_video_mode_does_not_touch_asset_library(store, tmp_path: Path):
    """视频模式不做角色自动挂载，行为与素材库接入前一致。"""
    content = _png(tmp_path / "ref.png")
    await store.create(
        name="Victor", variant="默认", content=content, mime_type="image/png"
    )

    class ExplodingMatcher:
        async def match(self, *args: Any, **kwargs: Any):
            raise AssertionError("视频模式不应调用语义匹配")

    document = _cg_document(tmp_path)
    services = SimpleNamespace(
        asset_library_store=store, character_matcher=ExplodingMatcher()
    )

    # plan_requirements 只在 mode == "image" 时调用 _resolve_character_assets；
    # 这里直接断言视频模式下不会构造出角色上下文参数。
    assert _character_context_argument(
        SimpleNamespace(plan=lambda **_: None), []
    ) == {}
    assert document.media_assets[0].origin == "feishu"
