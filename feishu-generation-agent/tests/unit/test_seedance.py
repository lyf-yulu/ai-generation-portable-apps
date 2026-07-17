import base64
from hashlib import sha256
from io import BytesIO
import json
import logging
import os
from pathlib import Path
from typing import Any

import httpx
from PIL import Image
import pytest
from pydantic import SecretStr

from feishu_generation_agent.domain.artifact import ProviderSubmission
from feishu_generation_agent.domain.document import MediaAsset
from feishu_generation_agent.domain.errors import AgentError, ErrorCategory
from feishu_generation_agent.domain.plan import GenerationTask
from feishu_generation_agent.integrations.seedance import SeedanceVideoGenerator


def _image_bytes(image_format: str, color: str) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (2, 2), color).save(buffer, format=image_format)
    return buffer.getvalue()


PNG_BLUE = _image_bytes("PNG", "blue")
PNG_RED = _image_bytes("PNG", "red")
JPEG_GREEN = _image_bytes("JPEG", "green")
FIXTURE_DIR = Path(__file__).parents[1] / "fixtures"


def _asset(
    tmp_path: Path,
    asset_id: str,
    *,
    content: bytes,
    mime_type: str,
) -> MediaAsset:
    extension = ".jpg" if mime_type == "image/jpeg" else ".png"
    path = tmp_path / f"{asset_id}{extension}"
    path.write_bytes(content)
    return MediaAsset(
        asset_id=asset_id,
        source_block_id=f"block-{asset_id}",
        origin="fictional_fixture",
        local_path=path,
        mime_type=mime_type,
        size=len(content),
        sha256=sha256(content).hexdigest(),
        width=2,
        height=2,
    )


def _assets(tmp_path: Path) -> list[MediaAsset]:
    return [
        _asset(
            tmp_path,
            "asset-blue",
            content=PNG_BLUE,
            mime_type="image/png",
        ),
        _asset(
            tmp_path,
            "asset-green",
            content=JPEG_GREEN,
            mime_type="image/jpeg",
        ),
    ]


def _video_task(
    *,
    references: list[dict] | None = None,
    output_count: int = 1,
) -> GenerationTask:
    return GenerationTask(
        task_id="task-video-fictional",
        task_type="image_to_video",
        title="纸船多镜头短片",
        source_block_ids=["shot-1", "shot-2"],
        user_intent="按两个镜头生成连续短片",
        prompt="镜头一：蓝色纸船漂近。镜头二：纸船驶向绿色河岸。",
        negative_constraints=["不要添加字幕", "不要改变纸船颜色"],
        reference_images=references
        or [
            {"asset_id": "asset-blue", "role": "reference_image", "order": 1},
            {"asset_id": "asset-green", "role": "reference_image", "order": 2},
        ],
        aspect_ratio="9:16",
        duration=10,
        resolution="720p",
        generate_audio=True,
        output_count=output_count,
    )


def _recording_client(
    requests: list[httpx.Request],
) -> httpx.AsyncClient:
    def create(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"id": "task-ark-fictional-123", "status": "queued"},
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(create))


@pytest.mark.asyncio
async def test_submit_preserves_explicit_reference_order_and_official_payload(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def create(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"id": "task-ark-fictional-123", "status": "queued"},
        )

    key = "fictional-ark-key-never-sent-externally"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(create)
    ) as client:
        generator = SeedanceVideoGenerator(
            client,
            base_url="https://ark.fictional.test/api/v3",
            api_key=key,
            model="doubao-seedance-fictional-model",
        )
        submission = await generator.submit(
            _video_task(
                references=[
                    {
                        "asset_id": "asset-green",
                        "role": "reference_image",
                        "order": 2,
                    },
                    {
                        "asset_id": "asset-blue",
                        "role": "reference_image",
                        "order": 1,
                    },
                ]
            ),
            list(reversed(_assets(tmp_path))),
            submission_id="client-correlation-only",
        )

    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert request.url.path == "/api/v3/contents/generations/tasks"
    assert request.headers["authorization"] == f"Bearer {key}"
    body = json.loads(request.content)
    assert set(body) == {
        "model",
        "content",
        "duration",
        "ratio",
        "resolution",
        "generate_audio",
        "watermark",
    }
    assert body["model"] == "doubao-seedance-fictional-model"
    assert body["duration"] == 10
    assert body["ratio"] == "9:16"
    assert body["resolution"] == "720p"
    assert body["generate_audio"] is True
    assert body["watermark"] is False
    assert body["content"][0]["type"] == "text"
    assert "镜头一" in body["content"][0]["text"]
    assert "不要添加字幕" in body["content"][0]["text"]
    image_parts = body["content"][1:]
    assert [part["role"] for part in image_parts] == [
        "reference_image",
        "reference_image",
    ]
    assert [
        part["image_url"]["url"] for part in image_parts
    ] == [
        "data:image/png;base64," + base64.b64encode(PNG_BLUE).decode("ascii"),
        "data:image/jpeg;base64," + base64.b64encode(JPEG_GREEN).decode("ascii"),
    ]
    assert "submission_id" not in request.content.decode("utf-8")
    assert submission.provider == "seedance"
    assert submission.provider_task_id == "task-ark-fictional-123"
    assert submission.provider_task_id != "client-correlation-only"
    assert submission.status == "queued"


@pytest.mark.asyncio
async def test_submit_accepts_origin_base_and_valid_first_last_frames(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def create(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"id": "task-ark-first-last", "status": "queued"},
        )

    task = _video_task(
        references=[
            {"asset_id": "asset-green", "role": "last_frame", "order": 2},
            {"asset_id": "asset-blue", "role": "first_frame", "order": 1},
        ]
    ).model_copy(update={"generate_audio": None})
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(create)
    ) as client:
        generator = SeedanceVideoGenerator(
            client,
            base_url="https://ark.fictional.test",
            api_key=SecretStr("fictional-key"),
            model="fictional-model",
        )
        result = await generator.submit(task, list(reversed(_assets(tmp_path))))

    assert result.provider_task_id == "task-ark-first-last"
    assert requests[0].url.path == "/api/v3/contents/generations/tasks"
    body = json.loads(requests[0].content)
    assert [item["role"] for item in body["content"][1:]] == [
        "first_frame",
        "last_frame",
    ]
    assert body["generate_audio"] is False


@pytest.mark.asyncio
async def test_origin_base_poll_uses_official_api_v3_path() -> None:
    requests: list[httpx.Request] = []

    def poll(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"id": "task-ark-origin", "status": "queued"},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(poll)
    ) as client:
        generator = SeedanceVideoGenerator(
            client,
            base_url="https://ark.fictional.test",
            api_key="fictional-key",
            model="fictional-model",
        )
        result = await generator.poll(
            _submission(provider_task_id="task-ark-origin")
        )

    assert result.status == "queued"
    assert requests[0].url.path == (
        "/api/v3/contents/generations/tasks/task-ark-origin"
    )


@pytest.mark.parametrize(
    ("updates", "field_name"),
    [
        ({"base_url": None}, "base_url"),
        ({"api_key": None}, "api_key"),
        ({"model": None}, "model"),
        ({"base_url": ""}, "base_url"),
        ({"base_url": "http://ark.fictional.test/api/v3"}, "base_url"),
        ({"base_url": "https://ark.fictional.test:8443/api/v3"}, "base_url"),
        ({"base_url": "https://ark.fictional.test:invalid/api/v3"}, "base_url"),
        ({"base_url": "https://user@ark.fictional.test/api/v3"}, "base_url"),
        ({"base_url": "https://ark.fictional.test/other"}, "base_url"),
        ({"base_url": "https://ark.fictional.test/api/v3?x=1"}, "base_url"),
        ({"base_url": "https://ark.fictional.test/api/v3#x"}, "base_url"),
        ({"base_url": "https://localhost/api/v3"}, "base_url"),
        ({"base_url": "https://ark.local/api/v3"}, "base_url"),
        ({"base_url": "https://127.0.0.1/api/v3"}, "base_url"),
        ({"base_url": "https://10.0.0.1/api/v3"}, "base_url"),
        ({"api_key": "  "}, "api_key"),
        ({"model": ""}, "model"),
        ({"max_response_bytes": 0}, "max_response_bytes"),
        ({"max_input_bytes": False}, "max_input_bytes"),
        ({"max_total_input_bytes": -1}, "max_total_input_bytes"),
        (
            {"max_input_bytes": 1024, "max_total_input_bytes": 512},
            "max_total_input_bytes",
        ),
    ],
)
def test_constructor_maps_invalid_configuration_to_safe_agent_error(
    updates: dict[str, Any],
    field_name: str,
) -> None:
    key = "fictional-key-must-not-appear"
    values: dict[str, Any] = {
        "base_url": "https://ark.fictional.test/api/v3",
        "api_key": key,
        "model": "fictional-model",
    }
    values.update(updates)

    with pytest.raises(AgentError) as caught:
        SeedanceVideoGenerator(httpx.AsyncClient(), **values)

    assert caught.value.detail.category == ErrorCategory.CONFIGURATION
    assert caught.value.detail.retryable is False
    assert field_name in caught.value.detail.technical_detail
    assert key not in str(caught.value.detail)


def test_constructor_keeps_string_key_secret_and_accepts_secretstr() -> None:
    string_key = "fictional-string-key-for-repr"
    secret_key = "fictional-secretstr-key-for-repr"
    string_generator = SeedanceVideoGenerator(
        httpx.AsyncClient(),
        base_url="https://ark.fictional.test/api/v3",
        api_key=string_key,
        model="fictional-model",
    )
    secret_generator = SeedanceVideoGenerator(
        httpx.AsyncClient(),
        base_url="https://ark.fictional.test/api/v3",
        api_key=SecretStr(secret_key),
        model="fictional-model",
    )

    assert string_key not in repr(vars(string_generator))
    assert secret_key not in repr(vars(secret_generator))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "image_task",
        "short_duration",
        "long_duration",
        "ratio",
        "resolution",
        "generate_audio",
        "output_count",
    ],
)
async def test_submit_rejects_invalid_video_parameters_before_http(
    tmp_path: Path,
    case: str,
) -> None:
    task = _video_task()
    if case == "image_task":
        task = GenerationTask(
            task_id="task-image",
            task_type="image_to_image",
            title="不应提交",
            source_block_ids=["block-1"],
            user_intent="不应提交",
            prompt="不应提交",
            reference_images=task.reference_images,
            aspect_ratio="9:16",
            image_size="2K",
        )
    elif case == "short_duration":
        task = task.model_copy(update={"duration": 3})
    elif case == "long_duration":
        task = task.model_copy(update={"duration": 16})
    elif case == "ratio":
        task = task.model_copy(update={"aspect_ratio": "auto"})
    elif case == "resolution":
        task = task.model_copy(update={"resolution": "8k"})
    elif case == "generate_audio":
        task = task.model_copy(update={"generate_audio": "yes"})
    else:
        task = task.model_copy(update={"output_count": 2})

    requests: list[httpx.Request] = []
    async with _recording_client(requests) as client:
        generator = SeedanceVideoGenerator(
            client,
            base_url="https://ark.fictional.test/api/v3",
            api_key="fictional-key",
            model="fictional-model",
        )
        with pytest.raises(AgentError) as caught:
            await generator.submit(task, _assets(tmp_path))

    assert caught.value.detail.category == ErrorCategory.VALIDATION
    assert caught.value.detail.retryable is False
    assert requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "missing_assets",
        "unknown",
        "duplicate_reference",
        "duplicate_order",
        "duplicate_input",
        "invalid_role",
        "mixed_roles",
        "last_without_first",
        "duplicate_first",
    ],
)
async def test_submit_rejects_invalid_reference_mapping_before_http(
    tmp_path: Path,
    case: str,
) -> None:
    references = [
        {"asset_id": "asset-blue", "role": "reference_image", "order": 1},
        {"asset_id": "asset-green", "role": "reference_image", "order": 2},
    ]
    assets = _assets(tmp_path)
    if case == "missing_assets":
        assets = []
    elif case == "unknown":
        references[1]["asset_id"] = "asset-missing"
    elif case == "duplicate_reference":
        references[1]["asset_id"] = "asset-blue"
    elif case == "duplicate_order":
        references[1]["order"] = 1
    elif case == "duplicate_input":
        assets[1] = assets[0]
    elif case == "invalid_role":
        references[0]["role"] = "reference_video"
    elif case == "mixed_roles":
        references[0]["role"] = "first_frame"
    elif case == "last_without_first":
        references = [
            {"asset_id": "asset-blue", "role": "last_frame", "order": 1}
        ]
        assets = assets[:1]
    elif case == "duplicate_first":
        references[0]["role"] = "first_frame"
        references[1]["role"] = "first_frame"

    task = _video_task(references=references)
    requests: list[httpx.Request] = []
    async with _recording_client(requests) as client:
        generator = SeedanceVideoGenerator(
            client,
            base_url="https://ark.fictional.test/api/v3",
            api_key="fictional-key",
            model="fictional-model",
        )
        with pytest.raises(AgentError) as caught:
            await generator.submit(task, assets)

    assert caught.value.detail.category == ErrorCategory.VALIDATION
    assert caught.value.detail.retryable is False
    assert requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "expected_category"),
    [
        ("download_error", ErrorCategory.DOCUMENT),
        ("missing_file", ErrorCategory.DOCUMENT),
        ("size", ErrorCategory.DOCUMENT),
        ("hash", ErrorCategory.DOCUMENT),
        ("declared_mime", ErrorCategory.VALIDATION),
        ("content_mime", ErrorCategory.DOCUMENT),
        ("symlink", ErrorCategory.DOCUMENT),
    ],
)
async def test_submit_rejects_invalid_or_unsafe_input_file(
    tmp_path: Path,
    case: str,
    expected_category: ErrorCategory,
) -> None:
    assets = _assets(tmp_path)
    first = assets[0]
    if case == "download_error":
        assets[0] = first.model_copy(update={"download_error": "fictional"})
    elif case == "missing_file":
        first.local_path.unlink()
    elif case == "size":
        assets[0] = first.model_copy(update={"size": first.size - 1})
    elif case == "hash":
        assets[0] = first.model_copy(update={"sha256": "0" * 64})
    elif case == "declared_mime":
        assets[0] = first.model_copy(update={"mime_type": "video/mp4"})
    elif case == "content_mime":
        assets[0] = first.model_copy(update={"mime_type": "image/jpeg"})
    else:
        target = tmp_path / "symlink-target.png"
        target.write_bytes(PNG_BLUE)
        first.local_path.unlink()
        first.local_path.symlink_to(target)

    requests: list[httpx.Request] = []
    async with _recording_client(requests) as client:
        generator = SeedanceVideoGenerator(
            client,
            base_url="https://ark.fictional.test/api/v3",
            api_key="fictional-key",
            model="fictional-model",
        )
        with pytest.raises(AgentError) as caught:
            await generator.submit(_video_task(), assets)

    assert caught.value.detail.category == expected_category
    assert caught.value.detail.retryable is False
    assert requests == []


@pytest.mark.asyncio
async def test_submit_enforces_single_and_total_input_limits_without_read_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_read_bytes(path: Path) -> bytes:
        raise AssertionError(f"unbounded read_bytes used for {path.name}")

    monkeypatch.setattr(Path, "read_bytes", forbidden_read_bytes)
    assets = _assets(tmp_path)
    cases = [
        {
            "max_input_bytes": assets[0].size - 1,
            "max_total_input_bytes": assets[0].size + assets[1].size,
        },
        {
            "max_input_bytes": max(asset.size for asset in assets),
            "max_total_input_bytes": sum(asset.size for asset in assets) - 1,
        },
    ]
    for limits in cases:
        requests: list[httpx.Request] = []
        async with _recording_client(requests) as client:
            generator = SeedanceVideoGenerator(
                client,
                base_url="https://ark.fictional.test/api/v3",
                api_key="fictional-key",
                model="fictional-model",
                **limits,
            )
            with pytest.raises(AgentError) as caught:
                await generator.submit(_video_task(), assets)
        assert caught.value.detail.category == ErrorCategory.DOCUMENT
        assert requests == []


@pytest.mark.asyncio
async def test_submit_detects_file_replacement_between_check_and_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assets = _assets(tmp_path)
    first_path = assets[0].local_path
    real_open = os.open
    replaced = False

    def replacing_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal replaced
        if not replaced and os.fspath(path) == os.fspath(first_path):
            replaced = True
            first_path.unlink()
            first_path.write_bytes(PNG_RED)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", replacing_open)
    requests: list[httpx.Request] = []
    async with _recording_client(requests) as client:
        generator = SeedanceVideoGenerator(
            client,
            base_url="https://ark.fictional.test/api/v3",
            api_key="fictional-key",
            model="fictional-model",
        )
        with pytest.raises(AgentError) as caught:
            await generator.submit(_video_task(), assets)

    assert replaced is True
    assert caught.value.detail.category == ErrorCategory.DOCUMENT
    assert requests == []


def _submission(
    *,
    provider: str = "seedance",
    provider_task_id: str = "task-ark-fictional-123",
    status: str = "queued",
) -> ProviderSubmission:
    return ProviderSubmission(
        provider=provider,
        provider_task_id=provider_task_id,
        status=status,
    )


def _generator_for_handler(
    handler: Any,
    *,
    key: str = "fictional-key",
    max_response_bytes: int = 1024 * 1024,
) -> tuple[SeedanceVideoGenerator, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return (
        SeedanceVideoGenerator(
            client,
            base_url="https://ark.fictional.test/api/v3",
            api_key=key,
            model="fictional-model",
            max_response_bytes=max_response_bytes,
        ),
        client,
    )


@pytest.mark.asyncio
async def test_poll_succeeded_returns_one_temporary_untrusted_signed_url() -> None:
    requests: list[httpx.Request] = []
    payload = json.loads(
        (FIXTURE_DIR / "seedance_succeeded.json").read_text(encoding="utf-8")
    )

    def poll(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=payload)

    generator, client = _generator_for_handler(poll)
    async with client:
        result = await generator.poll(_submission())

    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert requests[0].url.path == (
        "/api/v3/contents/generations/tasks/task-ark-fictional-123"
    )
    assert result.provider_task_id == "task-ark-fictional-123"
    assert result.status == "succeeded"
    assert len(result.result_items) == 1
    item = result.result_items[0]
    assert item.url == payload["content"]["video_url"]
    assert item.url_trust == "untrusted"
    assert item.mime_type == "video/mp4"
    assert item.local_path is None
    assert item.size is None
    assert item.sha256 is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result_payload",
    [
        {"content": {"video_url": "https://cdn.fictional.test/video.mp4"}},
        {"content": {"videoUrl": "https://cdn.fictional.test/video.mp4"}},
        {
            "content": [
                {
                    "type": "video_url",
                    "video_url": {
                        "url": "https://cdn.fictional.test/video.mp4"
                    },
                }
            ]
        },
        {
            "content": [
                {"videoUrl": "https://cdn.fictional.test/video.mp4"}
            ]
        },
        {
            "content": [
                {"url": "https://cdn.fictional.test/video.mp4"}
            ]
        },
        {
            "data": {
                "content": {
                    "video_url": "https://cdn.fictional.test/video.mp4"
                }
            }
        },
        {"video_url": "https://cdn.fictional.test/video.mp4"},
        {"videoUrl": "https://cdn.fictional.test/video.mp4"},
        {"results": [{"url": "https://cdn.fictional.test/video.mp4"}]},
        {
            "results": [
                {"video_url": "https://cdn.fictional.test/video.mp4"}
            ]
        },
    ],
)
async def test_poll_accepts_each_known_single_video_result_schema(
    result_payload: dict[str, Any],
) -> None:
    payload = {
        "id": "task-ark-fictional-123",
        "status": "succeeded",
        **result_payload,
    }
    generator, client = _generator_for_handler(
        lambda request: httpx.Response(200, json=payload)
    )
    async with client:
        result = await generator.poll(_submission())

    assert len(result.result_items) == 1
    assert result.result_items[0].url == (
        "https://cdn.fictional.test/video.mp4"
    )
    assert result.result_items[0].url_trust == "untrusted"


@pytest.mark.asyncio
async def test_poll_deduplicates_same_video_url_across_known_schema() -> None:
    video_url = "https://cdn.fictional.test/video.mp4?signature=fixture"
    generator, client = _generator_for_handler(
        lambda request: httpx.Response(
            200,
            json={
                "id": "task-ark-fictional-123",
                "status": "succeeded",
                "results": [{"url": video_url}, {"video_url": video_url}],
            },
        )
    )
    async with client:
        result = await generator.poll(_submission())

    assert [item.url for item in result.result_items] == [video_url]


@pytest.mark.asyncio
async def test_poll_rejects_two_distinct_video_urls() -> None:
    generator, client = _generator_for_handler(
        lambda request: httpx.Response(
            200,
            json={
                "id": "task-ark-fictional-123",
                "status": "succeeded",
                "content": [
                    {"url": "https://cdn.fictional.test/one.mp4"},
                    {"video_url": "https://cdn.fictional.test/two.mp4"},
                ],
            },
        )
    )
    async with client:
        with pytest.raises(AgentError) as caught:
            await generator.poll(_submission())

    assert caught.value.detail.category == ErrorCategory.PROVIDER_TERMINAL
    assert "multiple_video_urls" in caught.value.detail.technical_detail


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["success", "succeeded"])
async def test_submit_immediate_success_uses_same_video_result_contract(
    tmp_path: Path,
    status: str,
) -> None:
    video_url = "https://cdn.fictional.test/immediate.mp4?signature=fixture"
    generator, client = _generator_for_handler(
        lambda request: httpx.Response(
            200,
            json={
                "id": "task-ark-immediate",
                "status": status,
                "data": {"results": [{"video_url": video_url}]},
            },
        )
    )
    async with client:
        result = await generator.submit(_video_task(), _assets(tmp_path))

    assert result.status == "succeeded"
    assert [item.url for item in result.result_items] == [video_url]
    assert result.result_items[0].url_trust == "untrusted"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    ["queued", "running", "pending", "submitted", "processing", "in_progress"],
)
async def test_poll_nonterminal_status_performs_exactly_one_get(
    status: str,
) -> None:
    requests: list[httpx.Request] = []

    def poll(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"id": "task-ark-fictional-123", "status": status},
        )

    generator, client = _generator_for_handler(poll)
    async with client:
        result = await generator.poll(_submission())

    assert len(requests) == 1
    assert result.status == status
    assert result.result_items == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "cancelled", "canceled", "expired"])
async def test_poll_terminal_status_is_non_retryable_and_redacted(
    status: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    raw_secret = "provider-raw-secret-must-not-leak"

    def poll(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "task-ark-fictional-123",
                "status": status,
                "error": {"message": raw_secret},
            },
        )

    generator, client = _generator_for_handler(poll)
    with caplog.at_level(logging.DEBUG):
        async with client:
            with pytest.raises(AgentError) as caught:
                await generator.poll(_submission())

    assert caught.value.detail.category == ErrorCategory.PROVIDER_TERMINAL
    assert caught.value.detail.retryable is False
    assert status in caught.value.detail.technical_detail
    assert raw_secret not in str(caught.value.detail)
    assert raw_secret not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"status": "mystery"},
        {"id": "task-ark-fictional-123"},
    ],
)
async def test_poll_rejects_unknown_or_missing_status(payload: dict[str, str]) -> None:
    generator, client = _generator_for_handler(
        lambda request: httpx.Response(200, json=payload)
    )
    async with client:
        with pytest.raises(AgentError) as caught:
            await generator.poll(_submission())

    assert caught.value.detail.category == ErrorCategory.PROVIDER_TERMINAL
    assert caught.value.detail.retryable is False
    assert "status" in caught.value.detail.technical_detail


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "video_url",
    [
        "http://cdn.fictional.test/video.mp4",
        "data:video/mp4;base64,AAAA",
        "https://localhost/video.mp4",
        "https://service.localhost/video.mp4",
        "https://service.local/video.mp4",
        "https://service.internal/video.mp4",
        "https://127.0.0.1/video.mp4",
        "https://10.0.0.1/video.mp4",
        "https://[::1]/video.mp4",
        "https://user@cdn.fictional.test/video.mp4",
        "https://cdn.fictional.test:8443/video.mp4",
        "https://cdn.fictional.test/video.mp4#fragment",
    ],
)
async def test_poll_rejects_obviously_unsafe_result_url(video_url: str) -> None:
    generator, client = _generator_for_handler(
        lambda request: httpx.Response(
            200,
            json={
                "id": "task-ark-fictional-123",
                "status": "succeeded",
                "content": {"video_url": video_url},
            },
        )
    )
    async with client:
        with pytest.raises(AgentError) as caught:
            await generator.poll(_submission())

    assert caught.value.detail.category == ErrorCategory.PROVIDER_TERMINAL
    assert caught.value.detail.retryable is False
    assert video_url not in str(caught.value.detail)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        {},
        {"video_url": ""},
        {"video_url": 123},
        {
            "video_url": "https://cdn.fictional.test/video.mp4",
            "mime_type": "text/html",
        },
    ],
)
async def test_poll_rejects_missing_or_invalid_video_result(
    content: dict[str, Any],
) -> None:
    generator, client = _generator_for_handler(
        lambda request: httpx.Response(
            200,
            json={
                "id": "task-ark-fictional-123",
                "status": "succeeded",
                "content": content,
            },
        )
    )
    async with client:
        with pytest.raises(AgentError) as caught:
            await generator.poll(_submission())

    assert caught.value.detail.category == ErrorCategory.PROVIDER_TERMINAL
    assert caught.value.detail.retryable is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("submission", "expected_cause"),
    [
        (_submission(provider="other"), "provider"),
        (_submission(provider_task_id=""), "provider_task_id"),
        (_submission(provider_task_id="task\nid"), "provider_task_id"),
        (_submission(provider_task_id="task\x7fid"), "provider_task_id"),
        (_submission(provider_task_id="x" * 513), "provider_task_id"),
    ],
)
async def test_poll_rejects_foreign_or_unsafe_official_id_before_http(
    submission: ProviderSubmission,
    expected_cause: str,
) -> None:
    requests: list[httpx.Request] = []
    generator, client = _generator_for_handler(
        lambda request: requests.append(request) or httpx.Response(500)
    )
    async with client:
        with pytest.raises(AgentError) as caught:
            await generator.poll(submission)

    assert caught.value.detail.category == ErrorCategory.VALIDATION
    assert expected_cause in caught.value.detail.technical_detail
    assert requests == []


@pytest.mark.asyncio
async def test_poll_accepts_32_lowercase_hex_as_official_ark_id() -> None:
    official_id = "a" * 32
    requests: list[httpx.Request] = []

    def poll(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": official_id, "status": "queued"})

    generator, client = _generator_for_handler(poll)
    async with client:
        result = await generator.poll(_submission(provider_task_id=official_id))

    assert len(requests) == 1
    assert requests[0].url.path.endswith(f"/{official_id}")
    assert result.provider_task_id == official_id


@pytest.mark.asyncio
async def test_poll_quotes_official_task_id_as_one_path_segment() -> None:
    requests: list[httpx.Request] = []
    official_id = "task/id ?fictional"

    def poll(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": official_id, "status": "queued"})

    generator, client = _generator_for_handler(poll)
    async with client:
        result = await generator.poll(_submission(provider_task_id=official_id))

    assert len(requests) == 1
    assert requests[0].url.raw_path.endswith(b"task%2Fid%20%3Ffictional")
    assert result.provider_task_id == official_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "expected_cause"),
    [
        ({"status": "queued"}, "provider_task_id"),
        ({"id": "", "status": "queued"}, "provider_task_id"),
        ({"id": "task-ark", "status": "mystery"}, "status"),
    ],
)
async def test_submit_rejects_malformed_create_response(
    tmp_path: Path,
    payload: dict[str, str],
    expected_cause: str,
) -> None:
    generator, client = _generator_for_handler(
        lambda request: httpx.Response(200, json=payload)
    )
    async with client:
        with pytest.raises(AgentError) as caught:
            await generator.submit(_video_task(), _assets(tmp_path))

    assert caught.value.detail.category == ErrorCategory.PROVIDER_TERMINAL
    assert expected_cause in caught.value.detail.technical_detail


@pytest.mark.asyncio
async def test_submit_accepts_32_lowercase_hex_returned_as_official_ark_id(
    tmp_path: Path,
) -> None:
    official_id = "b" * 32
    generator, client = _generator_for_handler(
        lambda request: httpx.Response(
            200,
            json={"id": official_id, "status": "queued"},
        )
    )
    async with client:
        result = await generator.submit(
            _video_task(),
            _assets(tmp_path),
            submission_id="client-intent-correlation",
        )

    assert result.provider_task_id == official_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "category", "retryable"),
    [
        (401, ErrorCategory.PERMISSION, False),
        (403, ErrorCategory.PERMISSION, False),
        (400, ErrorCategory.PROVIDER_TERMINAL, False),
        (429, ErrorCategory.TRANSIENT, True),
        (500, ErrorCategory.TRANSIENT, True),
    ],
)
async def test_poll_maps_http_status_without_leaking_body_or_key(
    status_code: int,
    category: ErrorCategory,
    retryable: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    key = "fictional-auth-key-must-not-leak"
    raw_secret = "fictional-response-secret-must-not-leak"
    generator, client = _generator_for_handler(
        lambda request: httpx.Response(status_code, text=raw_secret),
        key=key,
    )
    with caplog.at_level(logging.DEBUG):
        async with client:
            with pytest.raises(AgentError) as caught:
                await generator.poll(_submission())

    assert caught.value.detail.category == category
    assert caught.value.detail.retryable is retryable
    assert key not in str(caught.value.detail)
    assert raw_secret not in str(caught.value.detail)
    assert key not in caplog.text
    assert raw_secret not in caplog.text


@pytest.mark.asyncio
async def test_poll_does_not_follow_redirect() -> None:
    requests: list[httpx.Request] = []

    def redirect(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            302,
            headers={"Location": "https://127.0.0.1/private"},
        )

    generator, client = _generator_for_handler(redirect)
    async with client:
        with pytest.raises(AgentError) as caught:
            await generator.poll(_submission())

    assert caught.value.detail.category == ErrorCategory.PROVIDER_TERMINAL
    assert requests and len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["invalid_json", "non_object", "too_large"])
async def test_poll_rejects_invalid_or_oversized_json(mode: str) -> None:
    if mode == "invalid_json":
        response = httpx.Response(200, content=b"not-json")
        limit = 1024
    elif mode == "non_object":
        response = httpx.Response(200, json=["not", "an", "object"])
        limit = 1024
    else:
        response = httpx.Response(200, content=b"{" + b"x" * 128 + b"}")
        del response.headers["content-length"]
        limit = 32
    generator, client = _generator_for_handler(
        lambda request: response,
        max_response_bytes=limit,
    )
    async with client:
        with pytest.raises(AgentError) as caught:
            await generator.poll(_submission())

    assert caught.value.detail.category == ErrorCategory.PROVIDER_TERMINAL
    assert caught.value.detail.retryable is False


@pytest.mark.asyncio
async def test_poll_maps_transport_failure_to_retryable_transient() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("fictional timeout", request=request)

    generator, client = _generator_for_handler(fail)
    async with client:
        with pytest.raises(AgentError) as caught:
            await generator.poll(_submission())

    assert caught.value.detail.category == ErrorCategory.TRANSIENT
    assert caught.value.detail.retryable is True
