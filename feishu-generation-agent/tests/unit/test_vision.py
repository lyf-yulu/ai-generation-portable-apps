import asyncio
import base64
from io import BytesIO
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from PIL import Image

from feishu_generation_agent.domain.document import (
    MediaAsset,
    VisionDescription,
)
from feishu_generation_agent.domain.errors import AgentError, ErrorCategory
from feishu_generation_agent.integrations.vision import ClaudeVisionAnalyzer
from feishu_generation_agent.storage.repository import Repository


def description_payload(asset_id: str = "asset-1") -> dict[str, Any]:
    return {
        "asset_id": asset_id,
        "subjects": ["一只熊猫玩偶"],
        "scene": "木质抽屉前",
        "style": "写实摄影",
        "composition": "主体位于画面中央",
        "characters": ["熊猫玩偶"],
        "actions": ["前爪搭在抽屉把手上"],
        "visible_text": ["OPEN"],
        "colors": ["黑色", "白色", "棕色"],
        "probable_role": "角色与场景参考图",
        "uncertainties": ["无法确认抽屉是否正在移动"],
    }


class FakeVisionModel:
    model_name = "claude-fictional-vision"

    def __init__(self, result: object | Exception | None = None) -> None:
        self.result = result if result is not None else description_payload()
        self.calls = 0
        self.structured_schema: type | None = None
        self.last_messages: list[dict[str, Any]] | None = None
        self.last_media_type: str | None = None
        self.last_image_data: str | None = None
        self.started = asyncio.Event()
        self.release: asyncio.Event | None = None

    def with_structured_output(self, schema: type) -> "FakeVisionModel":
        self.structured_schema = schema
        return self

    async def ainvoke(
        self,
        messages: list[dict[str, Any]],
    ) -> object:
        self.calls += 1
        self.last_messages = messages
        image_block = messages[1]["content"][0]
        self.last_media_type = image_block["source"]["media_type"]
        self.last_image_data = image_block["source"]["data"]
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class ModelRefusalError(RuntimeError):
    pass


class RateLimitFailure(RuntimeError):
    status_code = 429


@pytest.fixture
async def repository(tmp_path: Path):
    repo = await Repository.open(tmp_path / "agent.sqlite3")
    try:
        yield repo
    finally:
        await repo.close()


@pytest.fixture
def webp_asset(tmp_path: Path) -> MediaAsset:
    image = tmp_path / "reference.webp"
    image.write_bytes(b"RIFF" + b"x" * 40 + b"WEBPVP8 ")
    return MediaAsset(
        asset_id="asset-1",
        source_block_id="block-1",
        origin="feishu",
        local_path=image,
        mime_type="image/webp",
        size=image.stat().st_size,
        sha256="abc123",
    )


async def test_analyze_sends_original_mime_and_caches_by_hash(
    repository: Repository,
    webp_asset: MediaAsset,
):
    model = FakeVisionModel()
    analyzer = ClaudeVisionAnalyzer(
        model,
        repository,
        prompt_version="vision-v1",
    )

    first = await analyzer.analyze(webp_asset)
    second = await analyzer.analyze(webp_asset)

    assert first.asset_id == "asset-1"
    assert second == first
    assert model.calls == 1
    assert model.last_media_type == "image/webp"
    assert base64.b64decode(model.last_image_data or "") == (
        webp_asset.local_path.read_bytes()
    )
    assert model.structured_schema is VisionDescription


async def test_analyze_downscales_large_image_for_model_only(
    repository: Repository,
    tmp_path: Path,
):
    image_path = tmp_path / "large-reference.png"
    image = Image.effect_noise((1600, 2848), 100).convert("RGB")
    image.save(image_path, format="PNG")
    original_bytes = image_path.read_bytes()
    asset = MediaAsset(
        asset_id="asset-large",
        source_block_id="block-large",
        origin="feishu",
        local_path=image_path,
        mime_type="image/png",
        size=len(original_bytes),
        sha256="large-image-hash",
    )
    model = FakeVisionModel()
    analyzer = ClaudeVisionAnalyzer(model, repository, prompt_version="vision-v1")

    await analyzer.analyze(asset)

    prepared_bytes = base64.b64decode(model.last_image_data or "")
    with Image.open(BytesIO(prepared_bytes)) as prepared:
        width, height = prepared.size
        assert prepared.format == "JPEG"
    assert model.last_media_type == "image/jpeg"
    assert max(width, height) <= 1568
    assert width * height <= 1_150_000
    assert len(prepared_bytes) < len(original_bytes)
    assert image_path.read_bytes() == original_bytes


async def test_analyze_uses_strict_visible_content_system_prompt(
    repository: Repository,
    webp_asset: MediaAsset,
):
    model = FakeVisionModel()
    analyzer = ClaudeVisionAnalyzer(model, repository, prompt_version="vision-v1")

    await analyzer.analyze(webp_asset)

    assert model.last_messages is not None
    system_message = model.last_messages[0]
    assert system_message["role"] == "system"
    prompt = system_message["content"]
    assert "只描述图片中直接可见的内容" in prompt
    assert "不得推断" in prompt
    assert "剧情" in prompt
    assert "品牌" in prompt
    assert "人物身份" in prompt
    assert "visible_text" in prompt and "逐项抄录" in prompt
    assert "uncertainties" in prompt and "只能" in prompt


async def test_cache_key_is_exact_and_only_stores_description_json(
    repository: Repository,
    webp_asset: MediaAsset,
):
    secret = "fictional-api-key-must-not-persist"
    raw_response = "fictional-raw-model-response"
    result = description_payload() | {
        "api_key": secret,
        "raw_response": raw_response,
    }
    model = FakeVisionModel(result)
    analyzer = ClaudeVisionAnalyzer(model, repository, prompt_version="vision-v7")

    await analyzer.analyze(webp_asset)

    cached = await repository.get_vision_cache(
        "abc123:claude-fictional-vision:vision-v7"
    )
    assert cached is not None
    assert set(cached) == set(VisionDescription.model_fields)
    serialized = json.dumps(cached, ensure_ascii=False)
    assert model.last_image_data not in serialized
    assert raw_response not in serialized
    assert secret not in serialized


async def test_concurrent_same_key_uses_single_model_call(
    repository: Repository,
    webp_asset: MediaAsset,
):
    model = FakeVisionModel()
    model.release = asyncio.Event()
    analyzer = ClaudeVisionAnalyzer(model, repository, prompt_version="vision-v1")

    pending = [
        asyncio.create_task(analyzer.analyze(webp_asset)) for _ in range(5)
    ]
    await model.started.wait()
    await asyncio.sleep(0)
    model.release.set()
    results = await asyncio.gather(*pending)

    assert model.calls == 1
    assert results == [results[0]] * 5


async def test_concurrent_same_key_rebinds_each_callers_asset_identity(
    repository: Repository,
    webp_asset: MediaAsset,
):
    first_asset = webp_asset.model_copy(
        update={"asset_id": "asset-a", "source_block_id": "block-a"}
    )
    second_asset = webp_asset.model_copy(
        update={"asset_id": "asset-b", "source_block_id": "block-b"}
    )
    model = FakeVisionModel(description_payload("model-placeholder"))
    model.release = asyncio.Event()
    analyzer = ClaudeVisionAnalyzer(model, repository, prompt_version="vision-v1")

    pending = [
        asyncio.create_task(analyzer.analyze(first_asset)),
        asyncio.create_task(analyzer.analyze(second_asset)),
    ]
    await model.started.wait()
    await asyncio.sleep(0)
    model.release.set()
    results = await asyncio.gather(*pending)

    assert model.calls == 1
    assert [result.asset_id for result in results] == ["asset-a", "asset-b"]
    cached = await repository.get_vision_cache(
        "abc123:claude-fictional-vision:vision-v1"
    )
    assert cached is not None
    assert cached["asset_id"] == ""


async def test_concurrent_shared_failure_rebinds_safe_error_to_each_asset(
    repository: Repository,
    webp_asset: MediaAsset,
):
    first_asset = webp_asset.model_copy(update={"asset_id": "asset-a"})
    second_asset = webp_asset.model_copy(update={"asset_id": "asset-b"})
    model = FakeVisionModel(
        ModelRefusalError("fictional-secret-in-shared-refusal")
    )
    model.release = asyncio.Event()
    analyzer = ClaudeVisionAnalyzer(model, repository, prompt_version="vision-v1")

    pending = [
        asyncio.create_task(analyzer.analyze(first_asset)),
        asyncio.create_task(analyzer.analyze(second_asset)),
    ]
    await model.started.wait()
    await asyncio.sleep(0)
    model.release.set()
    results = await asyncio.gather(*pending, return_exceptions=True)

    assert model.calls == 1
    assert results[0] is not results[1]
    for asset, result in zip((first_asset, second_asset), results, strict=True):
        assert isinstance(result, AgentError)
        detail = result.detail
        serialized = json.dumps(detail.model_dump(mode="json"))
        assert detail.category == ErrorCategory.PROVIDER_TERMINAL
        assert detail.retryable is False
        assert asset.asset_id in f"{detail.message} {detail.technical_detail}"
        assert "fictional-secret" not in serialized
        assert result.__cause__ is None
        assert result.__context__ is None


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ConnectError(
            "fictional-secret-in-connection-error",
            request=httpx.Request("POST", "https://claude.invalid"),
        ),
        ConnectionError("fictional-secret-in-builtin-connection-error"),
        TimeoutError("fictional-secret-in-builtin-timeout-error"),
        RateLimitFailure("fictional-secret-in-rate-limit-error"),
    ],
)
async def test_connection_and_rate_limit_errors_are_retryable_and_safe(
    repository: Repository,
    webp_asset: MediaAsset,
    failure: Exception,
):
    model = FakeVisionModel(failure)
    analyzer = ClaudeVisionAnalyzer(model, repository, prompt_version="vision-v1")

    with pytest.raises(AgentError) as raised:
        await analyzer.analyze(webp_asset)

    detail = raised.value.detail
    assert detail.category == ErrorCategory.TRANSIENT
    assert detail.retryable is True
    assert webp_asset.asset_id in f"{detail.message} {detail.technical_detail}"
    assert "fictional-secret" not in json.dumps(detail.model_dump(mode="json"))
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert webp_asset.local_path.exists()


async def test_cache_connection_error_is_retryable_and_safe(
    repository: Repository,
    webp_asset: MediaAsset,
    monkeypatch: pytest.MonkeyPatch,
):
    async def fail_cache_read(cache_key: str) -> dict[str, Any] | None:
        del cache_key
        raise ConnectionError("fictional-secret-in-cache-error")

    monkeypatch.setattr(repository, "get_vision_cache", fail_cache_read)
    model = FakeVisionModel()
    analyzer = ClaudeVisionAnalyzer(model, repository, prompt_version="vision-v1")

    with pytest.raises(AgentError) as raised:
        await analyzer.analyze(webp_asset)

    detail = raised.value.detail
    assert detail.category == ErrorCategory.TRANSIENT
    assert detail.retryable is True
    assert webp_asset.asset_id in f"{detail.message} {detail.technical_detail}"
    assert "fictional-secret" not in json.dumps(detail.model_dump(mode="json"))
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert model.calls == 0


async def test_missing_structured_fields_are_non_retryable_validation_errors(
    repository: Repository,
    webp_asset: MediaAsset,
):
    result = description_payload()
    del result["scene"]
    model = FakeVisionModel(result)
    analyzer = ClaudeVisionAnalyzer(model, repository, prompt_version="vision-v1")

    with pytest.raises(AgentError) as raised:
        await analyzer.analyze(webp_asset)

    detail = raised.value.detail
    assert detail.category == ErrorCategory.VALIDATION
    assert detail.retryable is False
    assert webp_asset.asset_id in f"{detail.message} {detail.technical_detail}"
    assert webp_asset.local_path.exists()


async def test_none_structured_result_is_non_retryable_provider_refusal(
    repository: Repository,
    webp_asset: MediaAsset,
):
    model = FakeVisionModel()
    model.result = None
    analyzer = ClaudeVisionAnalyzer(model, repository, prompt_version="vision-v1")

    with pytest.raises(AgentError) as raised:
        await analyzer.analyze(webp_asset)

    detail = raised.value.detail
    assert detail.category == ErrorCategory.PROVIDER_TERMINAL
    assert detail.retryable is False
    assert "拒绝" in detail.message
    assert webp_asset.asset_id in f"{detail.message} {detail.technical_detail}"
    assert await repository.get_vision_cache(
        "abc123:claude-fictional-vision:vision-v1"
    ) is None


async def test_model_refusal_is_non_retryable_and_keeps_original_image(
    repository: Repository,
    webp_asset: MediaAsset,
):
    model = FakeVisionModel(
        ModelRefusalError("fictional-secret-in-refusal-response")
    )
    analyzer = ClaudeVisionAnalyzer(model, repository, prompt_version="vision-v1")

    with pytest.raises(AgentError) as raised:
        await analyzer.analyze(webp_asset)

    detail = raised.value.detail
    assert detail.category == ErrorCategory.PROVIDER_TERMINAL
    assert detail.retryable is False
    assert "拒绝" in detail.message
    assert webp_asset.asset_id in f"{detail.message} {detail.technical_detail}"
    assert "fictional-secret" not in json.dumps(detail.model_dump(mode="json"))
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert webp_asset.local_path.exists()


async def test_download_error_blocks_before_cache_file_and_model(
    repository: Repository,
    webp_asset: MediaAsset,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    sentinel = tmp_path / "existing-sentinel.webp"
    sentinel.write_bytes(b"must-not-be-read")
    failed_asset = webp_asset.model_copy(
        update={
            "asset_id": "asset-download-failed",
            "local_path": sentinel,
            "download_error": "fictional-secret-upstream-download-error",
        }
    )
    cache_reads = 0

    async def unexpected_cache_read(cache_key: str) -> dict[str, Any] | None:
        nonlocal cache_reads
        del cache_key
        cache_reads += 1
        return description_payload("cached-asset")

    monkeypatch.setattr(repository, "get_vision_cache", unexpected_cache_read)
    model = FakeVisionModel()
    analyzer = ClaudeVisionAnalyzer(model, repository, prompt_version="vision-v1")

    with pytest.raises(AgentError) as raised:
        await analyzer.analyze(failed_asset)

    detail = raised.value.detail
    serialized = json.dumps(detail.model_dump(mode="json"))
    assert detail.category == ErrorCategory.DOCUMENT
    assert detail.retryable is False
    assert failed_asset.asset_id in f"{detail.message} {detail.technical_detail}"
    assert "fictional-secret" not in serialized
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert cache_reads == 0
    assert model.calls == 0
    assert sentinel.exists()


async def test_missing_local_file_is_non_retryable_document_error(
    repository: Repository,
    webp_asset: MediaAsset,
    tmp_path: Path,
):
    missing_asset = webp_asset.model_copy(
        update={"local_path": tmp_path / "does-not-exist.webp"}
    )
    model = FakeVisionModel()
    analyzer = ClaudeVisionAnalyzer(model, repository, prompt_version="vision-v1")

    with pytest.raises(AgentError) as raised:
        await analyzer.analyze(missing_asset)

    detail = raised.value.detail
    assert detail.category == ErrorCategory.DOCUMENT
    assert detail.retryable is False
    assert missing_asset.asset_id in f"{detail.message} {detail.technical_detail}"
    assert model.calls == 0
