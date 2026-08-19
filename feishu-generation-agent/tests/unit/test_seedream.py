import base64
from pathlib import Path

import httpx
import pytest

from feishu_generation_agent.domain.document import MediaAsset
from feishu_generation_agent.domain.errors import AgentError
from feishu_generation_agent.domain.plan import GenerationTask
from feishu_generation_agent.integrations.seedream import (
    SeedreamImageGenerator,
    seedream_size,
)


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class _StubDownloader:
    def __init__(self, content: bytes = PNG_1X1) -> None:
        self.content = content
        self.calls: list[str] = []

    async def download(self, url: str, *, expected_mime_type: str) -> bytes:
        del expected_mime_type
        self.calls.append(url)
        return self.content


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
        sha256="a" * 64,
    )


def _task(**updates: object) -> GenerationTask:
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
        "image_provider": "seedream",
    }
    payload.update(updates)
    return GenerationTask.model_validate(payload)


def _generator(handler, tmp_path: Path, **updates) -> SeedreamImageGenerator:
    kwargs = {
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "api_key": "fictional-ark-key",
        "model": "doubao-seedream-5-0-pro-260628",
        "staging_dir": tmp_path / "staging",
        "result_downloader": _StubDownloader(),
        "max_result_bytes": 1024 * 1024,
    }
    kwargs.update(updates)
    return SeedreamImageGenerator(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)), **kwargs
    )


def test_seedream_size_maps_ratio_within_resolution():
    assert seedream_size("2K", "9:16") == "1584x2816"
    assert seedream_size("1K", "1:1") == "1024x1024"


def test_seedream_size_auto_returns_resolution_token():
    assert seedream_size("2K", "auto") == "2K"


def test_seedream_size_rejects_unsupported_resolution():
    with pytest.raises(ValueError):
        seedream_size("8K", "1:1")


def test_seedream_size_rejects_unsupported_ratio():
    with pytest.raises(ValueError):
        seedream_size("2K", "5:4")


async def test_submit_posts_to_ark_images_endpoint(tmp_path: Path):
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = request.read().decode()
        return httpx.Response(
            200, json={"data": [{"url": "https://cdn.example.com/a.png"}]}
        )

    generator = _generator(handler, tmp_path)

    await generator.submit(
        _task(), [_asset(tmp_path)], submission_id="a1b2c3d4" * 4
    )

    assert seen["url"] == (
        "https://ark.cn-beijing.volces.com/api/v3/images/generations"
    )
    assert seen["auth"] == "Bearer fictional-ark-key"
    body = str(seen["body"])
    assert "doubao-seedream-5-0-pro-260628" in body
    assert "1584x2816" in body
    assert "watermark" in body


async def test_submit_sends_reference_as_data_url(tmp_path: Path):
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.read().decode()
        return httpx.Response(
            200, json={"data": [{"url": "https://cdn.example.com/a.png"}]}
        )

    generator = _generator(handler, tmp_path)

    await generator.submit(
        _task(), [_asset(tmp_path)], submission_id="a1b2c3d4" * 4
    )

    assert "data:image/png;base64," in seen["body"]


async def test_submit_returns_succeeded_submission_with_staged_result(
    tmp_path: Path,
):
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200, json={"data": [{"url": "https://cdn.example.com/a.png"}]}
        )

    generator = _generator(handler, tmp_path)

    submission = await generator.submit(
        _task(), [_asset(tmp_path)], submission_id="a1b2c3d4" * 4
    )

    assert submission.provider == "seedream"
    assert submission.status == "succeeded"
    assert len(submission.result_items) == 1
    assert submission.result_items[0].mime_type == "image/png"


async def test_submit_accepts_base64_results(tmp_path: Path):
    encoded = base64.b64encode(PNG_1X1).decode()

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json={"data": [{"b64_json": encoded}]})

    generator = _generator(handler, tmp_path)

    submission = await generator.submit(
        _task(), [_asset(tmp_path)], submission_id="a1b2c3d4" * 4
    )

    assert submission.status == "succeeded"
    assert len(submission.result_items) == 1


async def test_submit_rejects_video_task(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("不应发起 HTTP 请求")

    generator = _generator(handler, tmp_path)
    video = GenerationTask.model_validate(
        {
            "task_id": "task-video",
            "task_type": "image_to_video",
            "title": "熊猫",
            "source_block_ids": ["block-1"],
            "user_intent": "动作",
            "prompt": "熊猫拉抽屉",
            "reference_images": [
                {"asset_id": "asset-1", "role": "reference_image", "order": 1}
            ],
            "aspect_ratio": "9:16",
            "duration": 5,
            "resolution": "720p",
        }
    )

    with pytest.raises(AgentError):
        await generator.submit(
            video, [_asset(tmp_path)], submission_id="a1b2c3d4" * 4
        )


async def test_empty_result_raises_agent_error(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json={"data": []})

    generator = _generator(handler, tmp_path)

    with pytest.raises(AgentError):
        await generator.submit(
            _task(), [_asset(tmp_path)], submission_id="a1b2c3d4" * 4
        )


async def test_http_error_raises_agent_error(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(429, json={"error": {"code": "RateLimit"}})

    generator = _generator(handler, tmp_path)

    with pytest.raises(AgentError):
        await generator.submit(
            _task(), [_asset(tmp_path)], submission_id="a1b2c3d4" * 4
        )


async def test_poll_is_terminal_passthrough(tmp_path: Path):
    """Seedream 是同步接口，submit 即终态，poll 原样返回。"""

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200, json={"data": [{"url": "https://cdn.example.com/a.png"}]}
        )

    generator = _generator(handler, tmp_path)
    submission = await generator.submit(
        _task(), [_asset(tmp_path)], submission_id="a1b2c3d4" * 4
    )

    assert await generator.poll(submission) is submission


async def test_api_key_never_leaks_into_error_message(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(500, text="upstream boom")

    generator = _generator(handler, tmp_path)

    with pytest.raises(AgentError) as excinfo:
        await generator.submit(
            _task(), [_asset(tmp_path)], submission_id="a1b2c3d4" * 4
        )

    rendered = f"{excinfo.value}{excinfo.value.detail.technical_detail}"
    assert "fictional-ark-key" not in rendered
