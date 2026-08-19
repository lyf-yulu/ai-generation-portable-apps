"""ChiyunImageGenerator 必须能以 registry 里的名字自报身份。

registry 用 banana / gpt-image2 作键，但生成器早期硬编码 provider="chiyun"。
nodes.py 的 `immediate.provider != provider` 校验因此失败，图明明出来了
却被判成「生成服务拒绝了请求」。
"""

import base64
from hashlib import sha256
from pathlib import Path

import httpx
import pytest

from feishu_generation_agent.domain.document import MediaAsset
from feishu_generation_agent.domain.plan import GenerationTask
from feishu_generation_agent.integrations.chiyun import ChiyunImageGenerator


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class _StubDownloader:
    async def download(self, url: str, *, expected_mime_type: str) -> bytes:
        del url, expected_mime_type
        return PNG_1X1


def _asset(tmp_path: Path) -> MediaAsset:
    path = tmp_path / "ref.png"
    path.write_bytes(PNG_1X1)
    return MediaAsset(
        asset_id="asset-1",
        source_block_id="block-1",
        origin="feishu",
        local_path=path,
        mime_type="image/png",
        size=len(PNG_1X1),
        # 生成器会做完整性校验，哈希必须与真实内容一致。
        sha256=sha256(PNG_1X1).hexdigest(),
    )


def _task() -> GenerationTask:
    return GenerationTask.model_validate(
        {
            "task_id": "task-cg",
            "task_type": "image_to_image",
            "title": "Victor",
            "source_block_ids": ["block-1"],
            "user_intent": "出 CG 图",
            "prompt": "@图片1 中的男性中景，戏剧化顶光",
            "reference_images": [
                {"asset_id": "asset-1", "role": "reference_image", "order": 1}
            ],
            "aspect_ratio": "9:16",
            "image_size": "2K",
        }
    )


def _gemini_response(request: httpx.Request) -> httpx.Response:
    del request
    return httpx.Response(
        200,
        json={
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "inlineData": {
                                    "mimeType": "image/png",
                                    "data": base64.b64encode(PNG_1X1).decode(),
                                }
                            }
                        ]
                    }
                }
            ]
        },
    )


def _generator(tmp_path: Path, **updates) -> ChiyunImageGenerator:
    kwargs = {
        "base_url": "https://chiyun.work",
        "api_key": "fictional-chiyun-key",
        "model": "banana2-ssvip",
        "staging_dir": tmp_path / "staging",
        "result_downloader": _StubDownloader(),
        "max_result_bytes": 1024 * 1024,
    }
    kwargs.update(updates)
    return ChiyunImageGenerator(
        httpx.AsyncClient(transport=httpx.MockTransport(_gemini_response)),
        **kwargs,
    )


async def test_defaults_to_chiyun_for_backward_compatibility(tmp_path: Path):
    """存量 run 的 provider 名已持久化为 chiyun，默认值不能变。"""
    generator = _generator(tmp_path)

    submission = await generator.submit(
        _task(), [_asset(tmp_path)], submission_id="a1b2c3d4" * 4
    )

    assert submission.provider == "chiyun"


@pytest.mark.parametrize("name", ["banana", "gpt-image2"])
async def test_reports_registry_name_when_given(tmp_path: Path, name: str):
    generator = _generator(tmp_path, provider_name=name)

    submission = await generator.submit(
        _task(), [_asset(tmp_path)], submission_id="a1b2c3d4" * 4
    )

    assert submission.provider == name


async def test_poll_reports_same_provider_name(tmp_path: Path):
    generator = _generator(tmp_path, provider_name="banana")

    submission = await generator.submit(
        _task(), [_asset(tmp_path)], submission_id="a1b2c3d4" * 4
    )
    polled = await generator.poll(submission)

    assert polled.provider == "banana"
