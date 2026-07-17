import base64
import json
import logging
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from PIL import Image

from feishu_generation_agent.domain.document import MediaAsset
from feishu_generation_agent.domain.errors import AgentError, ErrorCategory
from feishu_generation_agent.domain.artifact import ProviderSubmission
from feishu_generation_agent.domain.plan import GenerationTask
from feishu_generation_agent.integrations.chiyun import ChiyunImageGenerator
from feishu_generation_agent.integrations.safe_download import SafeResultDownloader


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _jpeg_fixture() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (1, 1), "green").save(buffer, format="JPEG")
    return buffer.getvalue()


JPEG_1X1 = _jpeg_fixture()


class _InlineFixtureDownloader:
    async def download(self, url: str, *, expected_mime_type: str) -> bytes:
        raise AssertionError(
            f"unexpected URL result {url} ({expected_mime_type})"
        )


class _SyncDownloader:
    def download(self, url: str, *, expected_mime_type: str) -> bytes:
        del url, expected_mime_type
        return b"not-awaitable"


INLINE_FIXTURE_DOWNLOADER = _InlineFixtureDownloader()


async def _public_result_resolver(host: str, port: int) -> list[str]:
    del host, port
    return ["93.184.216.34"]


def _task(*, output_count: int = 2) -> GenerationTask:
    return GenerationTask(
        task_id="task-image",
        task_type="image_to_image",
        title="虚构纸船海报",
        source_block_ids=["block-fictional"],
        user_intent="根据两张虚构参考图制作竖版海报",
        prompt="蓝色纸船漂过绿色河面",
        negative_constraints=["不要添加文字", "不要改变纸船颜色"],
        reference_images=[
            {"asset_id": "asset-blue", "role": "reference_image", "order": 1},
            {"asset_id": "asset-green", "role": "reference_image", "order": 2},
        ],
        aspect_ratio="9:16",
        image_size="2K",
        output_count=output_count,
    )


def _asset(
    tmp_path: Path,
    asset_id: str,
    content: bytes = PNG_1X1,
    mime_type: str = "image/png",
) -> MediaAsset:
    suffix = ".jpg" if mime_type == "image/jpeg" else ".png"
    path = tmp_path / f"{asset_id}{suffix}"
    path.write_bytes(content)
    return MediaAsset(
        asset_id=asset_id,
        source_block_id=f"block-{asset_id}",
        origin="fixture",
        local_path=path,
        mime_type=mime_type,
        size=len(content),
        sha256=sha256(content).hexdigest(),
        width=1,
        height=1,
    )


def _fixture_payload() -> dict[str, Any]:
    fixture = Path(__file__).parents[1] / "fixtures" / "chiyun_inline_response.json"
    return json.loads(fixture.read_text(encoding="utf-8"))


def _recording_client(
    requests: list[httpx.Request],
) -> httpx.AsyncClient:
    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_fixture_payload())

    return httpx.AsyncClient(transport=httpx.MockTransport(respond))


async def _submit_payload(
    tmp_path: Path,
    payload: dict[str, Any],
    *,
    status_code: int = 200,
    task_output_count: int = 1,
    submission_id: str | None = None,
    **generator_options: Any,
):
    requests: list[httpx.Request] = []
    generator_options.setdefault("staging_dir", tmp_path / "staging")
    generator_options.setdefault(
        "result_downloader",
        INLINE_FIXTURE_DOWNLOADER,
    )

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status_code, json=payload)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond)
    ) as client:
        generator = ChiyunImageGenerator(
            client,
            base_url="https://fictional-chiyun.test",
            api_key="fixture-key-never-sent-externally",
            model="fictional-model",
            **generator_options,
        )
        submit_options = (
            {"submission_id": submission_id}
            if submission_id is not None
            else {}
        )
        submission = await generator.submit(
            _task(output_count=task_output_count),
            [_asset(tmp_path, "asset-blue"), _asset(tmp_path, "asset-green")],
            **submit_options,
        )
    return submission, requests


@pytest.mark.asyncio
async def test_submit_uses_generate_content_original_mime_and_explicit_order(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_fixture_payload())

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond)
    ) as client:
        generator = ChiyunImageGenerator(
            client,
            base_url="https://fictional-chiyun.test",
            api_key="fixture-key-never-sent-externally",
            model="verified/model preview",
            staging_dir=tmp_path / "staging",
            result_downloader=INLINE_FIXTURE_DOWNLOADER,
        )
        assets = [
            _asset(tmp_path, "asset-blue"),
            _asset(
                tmp_path,
                "asset-green",
                JPEG_1X1,
                "image/jpeg",
            ),
        ]

        submission = await generator.submit(_task(), assets)

    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert request.url.raw_path == (
        b"/v1beta/models/verified%2Fmodel%20preview:generateContent"
    )
    assert request.headers["authorization"] == (
        "Bearer fixture-key-never-sent-externally"
    )
    body = json.loads(request.content)
    parts = body["contents"][0]["parts"]
    assert list(parts[0]) == ["text"]
    assert "不要添加文字" in parts[0]["text"]
    assert "生成 2 张" in parts[0]["text"]
    assert [part["inline_data"]["mime_type"] for part in parts[1:]] == [
        "image/png",
        "image/jpeg",
    ]
    assert [part["inline_data"]["data"] for part in parts[1:]] == [
        base64.b64encode(PNG_1X1).decode("ascii"),
        base64.b64encode(JPEG_1X1).decode("ascii"),
    ]
    assert body["generationConfig"] == {
        "imageConfig": {"aspectRatio": "9:16", "imageSize": "2K"}
    }
    assert submission.provider == "chiyun"
    assert submission.provider_task_id != "fictional-response-001"
    assert len(submission.provider_task_id) == 32
    assert submission.status == "succeeded"
    assert len(submission.result_items) == 2
    assert submission.result_items[0].mime_type == "image/png"
    assert submission.result_items[0].base64_data is None
    assert submission.result_items[0].url is None
    assert submission.result_items[0].local_path is not None
    assert submission.result_items[0].local_path.read_bytes() == b"fictional-image-result"


@pytest.mark.asyncio
async def test_submit_rejects_fewer_results_than_output_count(
    tmp_path: Path,
) -> None:
    payload = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "inlineData": {
                                "mimeType": "image/png",
                                "data": base64.b64encode(b"only-one").decode("ascii"),
                            }
                        }
                    ]
                }
            }
        ]
    }

    with pytest.raises(AgentError) as caught:
        await _submit_payload(tmp_path, payload, task_output_count=2)

    assert caught.value.detail.category == ErrorCategory.PROVIDER_TERMINAL
    assert caught.value.detail.retryable is False
    assert "result_count_mismatch" in caught.value.detail.technical_detail


@pytest.mark.asyncio
async def test_submit_truncates_extra_results_to_exact_output_count(
    tmp_path: Path,
) -> None:
    payload = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "inlineData": {
                                "mimeType": "image/png",
                                "data": base64.b64encode(f"result-{index}".encode()).decode("ascii"),
                            }
                        }
                        for index in range(3)
                    ]
                }
            }
        ]
    }

    submission, _ = await _submit_payload(
        tmp_path,
        payload,
        task_output_count=2,
    )

    assert submission.status == "succeeded"
    assert len(submission.result_items) == 2
    assert [
        item.local_path.read_bytes() for item in submission.result_items
    ] == [
        b"result-0",
        b"result-1",
    ]


@pytest.mark.parametrize(
    ("updates", "field_name"),
    [
        ({"base_url": "http://fictional-chiyun.test"}, "base_url"),
        ({"base_url": ""}, "base_url"),
        ({"api_key": "  "}, "api_key"),
        ({"model": ""}, "model"),
    ],
)
def test_constructor_rejects_unsafe_or_empty_configuration(
    tmp_path: Path,
    updates: dict[str, str],
    field_name: str,
) -> None:
    values = {
        "base_url": "https://fictional-chiyun.test",
        "api_key": "fixture-key-never-sent-externally",
        "model": "fictional-model",
        "staging_dir": tmp_path / "staging",
        "result_downloader": INLINE_FIXTURE_DOWNLOADER,
    }
    values.update(updates)

    with pytest.raises(AgentError) as caught:
        ChiyunImageGenerator(httpx.AsyncClient(), **values)

    assert caught.value.detail.category == ErrorCategory.CONFIGURATION
    assert caught.value.detail.retryable is False
    assert field_name in caught.value.detail.technical_detail
    if values["api_key"].strip():
        assert values["api_key"] not in str(caught.value.detail)


def test_constructor_keeps_api_key_secret_and_out_of_repr(
    tmp_path: Path,
) -> None:
    api_key = "fixture-secret-key-for-repr-test"
    generator = ChiyunImageGenerator(
        httpx.AsyncClient(),
        base_url="https://fictional-chiyun.test",
        api_key=SecretStr(api_key),
        model="fictional-model",
        staging_dir=tmp_path / "staging",
        result_downloader=INLINE_FIXTURE_DOWNLOADER,
    )

    assert api_key not in repr(generator)
    assert api_key not in repr(vars(generator))


@pytest.mark.parametrize(
    "invalid_downloader",
    [None, object(), _SyncDownloader()],
)
def test_constructor_requires_result_downloader_before_any_post(
    tmp_path: Path,
    invalid_downloader: object,
) -> None:
    requests: list[httpx.Request] = []

    with pytest.raises(AgentError) as caught:
        ChiyunImageGenerator(
            _recording_client(requests),
            base_url="https://fictional-chiyun.test",
            api_key="fixture-key",
            model="fictional-model",
            staging_dir=tmp_path / "staging",
            result_downloader=invalid_downloader,
        )

    assert caught.value.detail.category == ErrorCategory.CONFIGURATION
    assert "result_downloader" in caught.value.detail.technical_detail
    assert requests == []


@pytest.mark.parametrize(
    ("updates", "field_name"),
    [
        ({"base_url": None}, "base_url"),
        ({"api_key": None}, "api_key"),
        ({"model": None}, "model"),
        ({"staging_dir": None}, "staging_dir"),
        ({"base_url": "https://[::1"}, "base_url"),
        ({"base_url": "https://fictional.test:invalid"}, "base_url"),
        ({"base_url": "https://fictional.test:70000"}, "base_url"),
        ({"base_url": "https://user@fictional.test"}, "base_url"),
        ({"base_url": "https://fictional.test/path"}, "base_url"),
        ({"base_url": "https://fictional.test?query=1"}, "base_url"),
        ({"max_response_bytes": 0}, "max_response_bytes"),
        ({"max_result_bytes": -1}, "max_result_bytes"),
        ({"max_input_bytes": 0}, "max_input_bytes"),
        ({"max_total_input_bytes": -1}, "max_total_input_bytes"),
        (
            {"max_input_bytes": 1024, "max_total_input_bytes": 512},
            "max_total_input_bytes",
        ),
    ],
)
def test_constructor_maps_none_and_malformed_configuration_to_agent_error(
    tmp_path: Path,
    updates: dict[str, Any],
    field_name: str,
) -> None:
    key = "fixture-secret-never-in-errors"
    values: dict[str, Any] = {
        "base_url": "https://fictional-chiyun.test",
        "api_key": key,
        "model": "fictional-model",
        "staging_dir": tmp_path / "staging",
        "result_downloader": INLINE_FIXTURE_DOWNLOADER,
    }
    values.update(updates)

    with pytest.raises(AgentError) as caught:
        ChiyunImageGenerator(httpx.AsyncClient(), **values)

    assert caught.value.detail.category == ErrorCategory.CONFIGURATION
    assert caught.value.detail.retryable is False
    assert field_name in caught.value.detail.technical_detail
    assert key not in str(caught.value.detail)


@pytest.mark.asyncio
async def test_submit_rejects_single_input_over_limit_before_http(
    tmp_path: Path,
) -> None:
    first = _asset(tmp_path, "asset-blue", b"x" * 65)
    second = _asset(tmp_path, "asset-green", b"y" * 8)
    requests: list[httpx.Request] = []
    async with _recording_client(requests) as client:
        generator = ChiyunImageGenerator(
            client,
            base_url="https://fictional-chiyun.test",
            api_key="fixture-key",
            model="fictional-model",
            staging_dir=tmp_path / "staging",
            result_downloader=INLINE_FIXTURE_DOWNLOADER,
            max_input_bytes=64,
            max_total_input_bytes=128,
        )
        with pytest.raises(AgentError) as caught:
            await generator.submit(_task(output_count=1), [first, second])

    assert caught.value.detail.category == ErrorCategory.DOCUMENT
    assert "input_too_large" in caught.value.detail.technical_detail
    assert requests == []


@pytest.mark.asyncio
async def test_submit_rejects_total_inputs_before_read_bytes_or_http(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _asset(tmp_path, "asset-blue", b"x" * 60)
    second = _asset(tmp_path, "asset-green", b"y" * 60)

    def forbidden_read_bytes(path: Path) -> bytes:
        raise AssertionError(f"unbounded read_bytes used for {path.name}")

    monkeypatch.setattr(Path, "read_bytes", forbidden_read_bytes)
    requests: list[httpx.Request] = []
    async with _recording_client(requests) as client:
        generator = ChiyunImageGenerator(
            client,
            base_url="https://fictional-chiyun.test",
            api_key="fixture-key",
            model="fictional-model",
            staging_dir=tmp_path / "staging",
            result_downloader=INLINE_FIXTURE_DOWNLOADER,
            max_input_bytes=100,
            max_total_input_bytes=100,
        )
        with pytest.raises(AgentError) as caught:
            await generator.submit(_task(output_count=1), [first, second])

    assert caught.value.detail.category == ErrorCategory.DOCUMENT
    assert "total_input_too_large" in caught.value.detail.technical_detail
    assert requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["declared_size", "symlink_replacement"])
async def test_submit_rejects_forged_or_replaced_input_file(
    tmp_path: Path,
    case: str,
) -> None:
    first = _asset(tmp_path, "asset-blue")
    second = _asset(tmp_path, "asset-green")
    if case == "declared_size":
        first = first.model_copy(update={"size": first.size - 1})
    else:
        target = tmp_path / "replacement.png"
        target.write_bytes(PNG_1X1)
        first.local_path.unlink()
        first.local_path.symlink_to(target)

    requests: list[httpx.Request] = []
    async with _recording_client(requests) as client:
        generator = ChiyunImageGenerator(
            client,
            base_url="https://fictional-chiyun.test",
            api_key="fixture-key",
            model="fictional-model",
            staging_dir=tmp_path / "staging",
            result_downloader=INLINE_FIXTURE_DOWNLOADER,
            max_input_bytes=1024,
            max_total_input_bytes=2048,
        )
        with pytest.raises(AgentError) as caught:
            await generator.submit(_task(output_count=1), [first, second])

    assert caught.value.detail.category == ErrorCategory.DOCUMENT
    assert requests == []


@pytest.mark.asyncio
async def test_submit_rejects_non_image_task_before_http(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []
    video_task = GenerationTask(
        **(
            _task(output_count=1).model_dump()
            | {
                "task_type": "image_to_video",
                "image_size": None,
                "duration": 5,
                "resolution": "720p",
            }
        )
    )
    async with _recording_client(requests) as client:
        generator = ChiyunImageGenerator(
            client,
            base_url="https://fictional-chiyun.test",
            api_key="fixture-key",
            model="fictional-model",
            staging_dir=tmp_path / "staging",
            result_downloader=INLINE_FIXTURE_DOWNLOADER,
        )
        with pytest.raises(AgentError) as caught:
            await generator.submit(
                video_task,
                [_asset(tmp_path, "asset-blue"), _asset(tmp_path, "asset-green")],
            )

    assert caught.value.detail.category == ErrorCategory.VALIDATION
    assert caught.value.detail.retryable is False
    assert requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    ["unknown", "duplicate", "order", "duplicate_order", "role", "asset_order"],
)
async def test_submit_rejects_reference_identity_and_order_mismatch(
    tmp_path: Path,
    case: str,
) -> None:
    task = _task(output_count=1)
    assets = [_asset(tmp_path, "asset-blue"), _asset(tmp_path, "asset-green")]
    if case == "unknown":
        task = GenerationTask.model_validate(
            task.model_dump(mode="json")
            | {
                "reference_images": [
                    {"asset_id": "asset-blue", "role": "reference_image", "order": 1},
                    {"asset_id": "asset-missing", "role": "reference_image", "order": 2},
                ]
            }
        )
    elif case == "duplicate":
        task = GenerationTask.model_validate(
            task.model_dump(mode="json")
            | {
                "reference_images": [
                    {"asset_id": "asset-blue", "role": "reference_image", "order": 1},
                    {"asset_id": "asset-blue", "role": "reference_image", "order": 2},
                ]
            }
        )
    elif case == "order":
        task = GenerationTask.model_validate(
            task.model_dump(mode="json")
            | {
                "reference_images": [
                    {"asset_id": "asset-blue", "role": "reference_image", "order": 2},
                    {"asset_id": "asset-green", "role": "reference_image", "order": 1},
                ]
            }
        )
    elif case == "duplicate_order":
        task = GenerationTask.model_validate(
            task.model_dump(mode="json")
            | {
                "reference_images": [
                    {"asset_id": "asset-blue", "role": "reference_image", "order": 1},
                    {"asset_id": "asset-green", "role": "reference_image", "order": 1},
                ]
            }
        )
    elif case == "role":
        task = GenerationTask.model_validate(
            task.model_dump(mode="json")
            | {
                "reference_images": [
                    {"asset_id": "asset-blue", "role": "first_frame", "order": 1},
                    {"asset_id": "asset-green", "role": "reference_image", "order": 2},
                ]
            }
        )
    else:
        assets.reverse()

    requests: list[httpx.Request] = []
    async with _recording_client(requests) as client:
        generator = ChiyunImageGenerator(
            client,
            base_url="https://fictional-chiyun.test",
            api_key="fixture-key",
            model="fictional-model",
            staging_dir=tmp_path / "staging",
            result_downloader=INLINE_FIXTURE_DOWNLOADER,
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
        ("hash", ErrorCategory.DOCUMENT),
        ("non_image", ErrorCategory.VALIDATION),
    ],
)
async def test_submit_rejects_invalid_media_asset_before_http(
    tmp_path: Path,
    case: str,
    expected_category: ErrorCategory,
) -> None:
    first = _asset(tmp_path, "asset-blue")
    second = _asset(tmp_path, "asset-green")
    if case == "download_error":
        first = first.model_copy(update={"download_error": "fictional failure"})
    elif case == "missing_file":
        first.local_path.unlink()
    elif case == "hash":
        first = first.model_copy(update={"sha256": "0" * 64})
    else:
        first = first.model_copy(update={"mime_type": "video/mp4"})

    requests: list[httpx.Request] = []
    async with _recording_client(requests) as client:
        generator = ChiyunImageGenerator(
            client,
            base_url="https://fictional-chiyun.test",
            api_key="fixture-key",
            model="fictional-model",
            staging_dir=tmp_path / "staging",
            result_downloader=INLINE_FIXTURE_DOWNLOADER,
        )
        with pytest.raises(AgentError) as caught:
            await generator.submit(_task(output_count=1), [first, second])

    assert caught.value.detail.category == expected_category
    assert caught.value.detail.retryable is False
    assert requests == []


@pytest.mark.asyncio
async def test_submit_parses_snake_inline_and_https_url_results(
    tmp_path: Path,
) -> None:
    snake_data = base64.b64encode(b"fictional-webp-result").decode("ascii")
    payload = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "inline_data": {
                                "mime_type": "image/webp",
                                "data": snake_data,
                            }
                        },
                        {
                            "fileData": {
                                "mimeType": "image/jpeg",
                                "fileUri": "https://cdn.fictional.test/result.jpg?sig=fake",
                            }
                        },
                    ]
                }
            }
        ]
    }

    download_requests: list[httpx.Request] = []

    async def resolver(host: str, port: int) -> list[str]:
        assert (host, port) == ("cdn.fictional.test", 443)
        return ["93.184.216.34"]

    def download(request: httpx.Request) -> httpx.Response:
        download_requests.append(request)
        return httpx.Response(
            200,
            content=JPEG_1X1,
            headers={"Content-Type": "image/jpeg"},
        )

    result_downloader = SafeResultDownloader(
        transport=httpx.MockTransport(download),
        resolver=resolver,
        max_bytes=1024 * 1024,
    )
    result_downloader._http_client.headers["Authorization"] = (
        "Bearer must-not-reach-result-host"
    )
    try:
        submission, _ = await _submit_payload(
            tmp_path,
            payload,
            task_output_count=2,
            result_downloader=result_downloader,
        )
    finally:
        await result_downloader.aclose()

    assert [item.mime_type for item in submission.result_items] == [
        "image/webp",
        "image/jpeg",
    ]
    assert submission.result_items[0].local_path is not None
    assert submission.result_items[0].local_path.read_bytes() == b"fictional-webp-result"
    assert submission.result_items[1].local_path is not None
    assert submission.result_items[1].local_path.read_bytes() == JPEG_1X1
    assert len(download_requests) == 1
    assert "authorization" not in download_requests[0].headers
    staged_bytes = b"".join(
        path.read_bytes()
        for path in (tmp_path / "staging").rglob("*")
        if path.is_file()
    )
    assert b"sig=fake" not in staged_bytes
    assert b"cdn.fictional.test" not in staged_bytes


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"candidates": [{"content": {"parts": [{"text": "no image"}]}}]},
        {
            "candidates": [
                {"content": {"parts": [{"inlineData": {"mimeType": "text/plain", "data": "YWJj"}}]}}
            ]
        },
        {
            "candidates": [
                {"content": {"parts": [{"inlineData": {"mimeType": "image/png", "data": "%%%not-base64%%%"}}]}}
            ]
        },
        {
            "candidates": [
                {"content": {"parts": [{"inlineData": {"mimeType": "image/png", "data": ""}}]}}
            ]
        },
        {
            "candidates": [
                {"content": {"parts": [{"fileData": {"mimeType": "image/png", "fileUri": "http://cdn.fictional.test/out.png"}}]}}
            ]
        },
        {
            "candidates": [
                {"content": {"parts": [{"fileData": {"mimeType": "image/png", "fileUri": "data:image/png;base64,YWJj"}}]}}
            ]
        },
    ],
    ids=[
        "missing",
        "invalid-mime",
        "invalid-base64",
        "empty-data",
        "http-url",
        "data-url",
    ],
)
async def test_submit_rejects_missing_or_unsafe_provider_results(
    tmp_path: Path,
    payload: dict[str, Any],
) -> None:
    with pytest.raises(AgentError) as caught:
        await _submit_payload(tmp_path, payload)

    assert caught.value.detail.category == ErrorCategory.PROVIDER_TERMINAL
    assert caught.value.detail.retryable is False
    assert "data:image" not in str(caught.value.detail)
    assert "%%%not-base64%%%" not in str(caught.value.detail)


@pytest.mark.asyncio
async def test_submit_rejects_oversized_decoded_image(tmp_path: Path) -> None:
    payload = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "inlineData": {
                                "mimeType": "image/png",
                                "data": base64.b64encode(b"12345").decode("ascii"),
                            }
                        }
                    ]
                }
            }
        ]
    }

    with pytest.raises(AgentError) as caught:
        await _submit_payload(tmp_path, payload, max_result_bytes=4)

    assert caught.value.detail.category == ErrorCategory.PROVIDER_TERMINAL
    assert caught.value.detail.retryable is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "category", "retryable"),
    [
        (429, ErrorCategory.TRANSIENT, True),
        (503, ErrorCategory.TRANSIENT, True),
        (401, ErrorCategory.PERMISSION, False),
        (400, ErrorCategory.PROVIDER_TERMINAL, False),
    ],
)
async def test_submit_classifies_http_errors_without_raw_body(
    tmp_path: Path,
    status_code: int,
    category: ErrorCategory,
    retryable: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret_key = "fixture-secret-key-http-error"
    raw_secret = "raw-provider-body-with-base64-YWJjZA=="

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, text=raw_secret)

    caplog.set_level(logging.DEBUG)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond)
    ) as client:
        generator = ChiyunImageGenerator(
            client,
            base_url="https://fictional-chiyun.test",
            api_key=secret_key,
            model="fictional-model",
            staging_dir=tmp_path / "staging",
            result_downloader=INLINE_FIXTURE_DOWNLOADER,
        )
        with pytest.raises(AgentError) as caught:
            await generator.submit(
                _task(output_count=1),
                [_asset(tmp_path, "asset-blue"), _asset(tmp_path, "asset-green")],
            )

    assert caught.value.detail.category == category
    assert caught.value.detail.retryable is retryable
    combined = str(caught.value.detail) + caplog.text
    assert secret_key not in combined
    assert raw_secret not in combined


@pytest.mark.asyncio
async def test_submit_classifies_transport_timeout_as_retryable(
    tmp_path: Path,
) -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("fictional timeout", request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(timeout)
    ) as client:
        generator = ChiyunImageGenerator(
            client,
            base_url="https://fictional-chiyun.test",
            api_key="fixture-key",
            model="fictional-model",
            staging_dir=tmp_path / "staging",
            result_downloader=INLINE_FIXTURE_DOWNLOADER,
        )
        with pytest.raises(AgentError) as caught:
            await generator.submit(
                _task(output_count=1),
                [_asset(tmp_path, "asset-blue"), _asset(tmp_path, "asset-green")],
            )

    assert caught.value.detail.category == ErrorCategory.TRANSIENT
    assert caught.value.detail.retryable is True
    assert "ConnectTimeout" in caught.value.detail.technical_detail


@pytest.mark.asyncio
async def test_submit_limits_response_before_json_parsing(tmp_path: Path) -> None:
    def oversized(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'{' + b'"padding":"' + b'x' * 100 + b'"}')

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(oversized)
    ) as client:
        generator = ChiyunImageGenerator(
            client,
            base_url="https://fictional-chiyun.test",
            api_key="fixture-key",
            model="fictional-model",
            staging_dir=tmp_path / "staging",
            result_downloader=INLINE_FIXTURE_DOWNLOADER,
            max_response_bytes=32,
        )
        with pytest.raises(AgentError) as caught:
            await generator.submit(
                _task(output_count=1),
                [_asset(tmp_path, "asset-blue"), _asset(tmp_path, "asset-green")],
            )

    assert caught.value.detail.category == ErrorCategory.PROVIDER_TERMINAL
    assert caught.value.detail.retryable is False


@pytest.mark.asyncio
async def test_success_log_is_metadata_only_and_poll_does_not_request_again(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    key = "fixture-key-log-test"
    encoded = _fixture_payload()["candidates"][0]["content"]["parts"][0][
        "inlineData"
    ]["data"]
    requests: list[httpx.Request] = []
    caplog.set_level(logging.INFO)
    async with _recording_client(requests) as client:
        generator = ChiyunImageGenerator(
            client,
            base_url="https://fictional-chiyun.test",
            api_key=key,
            model="fictional-model",
            staging_dir=tmp_path / "staging",
            result_downloader=INLINE_FIXTURE_DOWNLOADER,
        )
        submission = await generator.submit(
            _task(output_count=1),
            [_asset(tmp_path, "asset-blue"), _asset(tmp_path, "asset-green")],
        )
        polled = await generator.poll(submission)

    assert polled == submission
    assert len(requests) == 1
    assert "result_count=1" in caplog.text
    assert "image/png" in caplog.text
    assert key not in caplog.text
    assert encoded not in caplog.text


@pytest.mark.asyncio
async def test_poll_recovers_synchronous_results_after_process_restart(
    tmp_path: Path,
) -> None:
    staging_dir = tmp_path / "staging"
    first_submission, submit_requests = await _submit_payload(
        tmp_path,
        _fixture_payload(),
        task_output_count=2,
        staging_dir=staging_dir,
    )
    checkpoint_submission = ProviderSubmission(
        provider="chiyun",
        provider_task_id=first_submission.provider_task_id,
        status="succeeded",
        result_items=[],
    )
    poll_requests: list[httpx.Request] = []

    def paid_post_must_not_repeat(request: httpx.Request) -> httpx.Response:
        poll_requests.append(request)
        raise AssertionError("poll attempted a second paid request")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(paid_post_must_not_repeat)
    ) as new_client:
        restarted = ChiyunImageGenerator(
            new_client,
            base_url="https://fictional-chiyun.test",
            api_key="different-runtime-key",
            model="fictional-model",
            staging_dir=staging_dir,
            result_downloader=INLINE_FIXTURE_DOWNLOADER,
        )
        recovered = await restarted.poll(checkpoint_submission)

    assert len(submit_requests) == 1
    assert poll_requests == []
    assert recovered.status == "succeeded"
    assert len(recovered.result_items) == 2
    assert [item.local_path.read_bytes() for item in recovered.result_items] == [
        b"fictional-image-result",
        b"fictional-image-result-2",
    ]
    assert all(item.base64_data is None for item in recovered.result_items)
    assert all(item.url is None for item in recovered.result_items)

    manifest = (
        staging_dir / first_submission.provider_task_id / "manifest.json"
    ).read_text(encoding="utf-8")
    assert set(json.loads(manifest)) == {"version", "results"}
    forbidden = (
        "fixture-key-never-sent-externally",
        "different-runtime-key",
        "ZmljdGlvbmFsLWltYWdlLXJlc3VsdA==",
        "https://",
    )
    all_staged = b"".join(
        path.read_bytes() for path in staging_dir.rglob("*") if path.is_file()
    )
    assert all(value.encode() not in all_staged for value in forbidden)


@pytest.mark.asyncio
async def test_persisted_submission_id_recovers_when_return_value_was_not_saved(
    tmp_path: Path,
) -> None:
    submission_id = "a" * 32
    staging_dir = tmp_path / "staging"
    returned, submit_requests = await _submit_payload(
        tmp_path,
        _fixture_payload(),
        task_output_count=2,
        submission_id=submission_id,
        staging_dir=staging_dir,
    )
    assert returned.provider_task_id == submission_id

    poll_requests: list[httpx.Request] = []
    async with _recording_client(poll_requests) as client:
        restarted = ChiyunImageGenerator(
            client,
            base_url="https://fictional-chiyun.test",
            api_key="different-runtime-key",
            model="fictional-model",
            staging_dir=staging_dir,
            result_downloader=INLINE_FIXTURE_DOWNLOADER,
        )
        recovered = await restarted.poll(
            ProviderSubmission(
                provider="chiyun",
                provider_task_id=submission_id,
                status="submitted",
                result_items=[],
            )
        )

    assert len(submit_requests) == 1
    assert poll_requests == []
    assert recovered.provider_task_id == submission_id
    assert len(recovered.result_items) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "submission_id",
    ["../outside", "A" * 32, "a" * 31, "a" * 33],
)
async def test_submit_rejects_unsafe_submission_id_before_paid_post(
    tmp_path: Path,
    submission_id: str,
) -> None:
    requests: list[httpx.Request] = []
    async with _recording_client(requests) as client:
        generator = ChiyunImageGenerator(
            client,
            base_url="https://fictional-chiyun.test",
            api_key="fixture-key",
            model="fictional-model",
            staging_dir=tmp_path / "staging",
            result_downloader=INLINE_FIXTURE_DOWNLOADER,
        )
        with pytest.raises(AgentError) as caught:
            await generator.submit(
                _task(output_count=1),
                [_asset(tmp_path, "asset-blue"), _asset(tmp_path, "asset-green")],
                submission_id=submission_id,
            )

    assert caught.value.detail.category == ErrorCategory.VALIDATION
    assert requests == []


@pytest.mark.asyncio
async def test_poll_missing_preassociated_staging_fails_without_post(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []
    async with _recording_client(requests) as client:
        generator = ChiyunImageGenerator(
            client,
            base_url="https://fictional-chiyun.test",
            api_key="fixture-key",
            model="fictional-model",
            staging_dir=tmp_path / "staging",
            result_downloader=INLINE_FIXTURE_DOWNLOADER,
        )
        with pytest.raises(AgentError) as caught:
            await generator.poll(
                ProviderSubmission(
                    provider="chiyun",
                    provider_task_id="b" * 32,
                    status="submitted",
                    result_items=[],
                )
            )

    assert caught.value.detail.category == ErrorCategory.PROVIDER_TERMINAL
    assert "staging_invalid" in caught.value.detail.technical_detail
    assert requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tamper",
    ["missing_manifest", "missing_file", "changed_file", "path_traversal"],
)
async def test_poll_fails_safely_for_missing_or_tampered_staging(
    tmp_path: Path,
    tamper: str,
) -> None:
    staging_dir = tmp_path / "staging"
    submission, _ = await _submit_payload(
        tmp_path,
        _fixture_payload(),
        task_output_count=2,
        staging_dir=staging_dir,
    )
    result_dir = staging_dir / submission.provider_task_id
    manifest_path = result_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    first_path = result_dir / manifest["results"][0]["filename"]
    if tamper == "missing_manifest":
        manifest_path.unlink()
    elif tamper == "missing_file":
        first_path.unlink()
    elif tamper == "changed_file":
        first_path.write_bytes(b"tampered")
    else:
        manifest["results"][0]["filename"] = "../outside.png"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    requests: list[httpx.Request] = []
    async with _recording_client(requests) as client:
        restarted = ChiyunImageGenerator(
            client,
            base_url="https://fictional-chiyun.test",
            api_key="fixture-key",
            model="fictional-model",
            staging_dir=staging_dir,
            result_downloader=INLINE_FIXTURE_DOWNLOADER,
        )
        with pytest.raises(AgentError) as caught:
            await restarted.poll(
                ProviderSubmission(
                    provider="chiyun",
                    provider_task_id=submission.provider_task_id,
                    status="succeeded",
                    result_items=[],
                )
            )

    assert caught.value.detail.category == ErrorCategory.PROVIDER_TERMINAL
    assert caught.value.detail.retryable is False
    assert requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unsafe_url",
    [
        "https://localhost/out.png",
        "https://127.0.0.1/out.png",
        "https://10.1.2.3/out.png",
        "https://169.254.169.254/latest/meta-data",
        "https://[::1]/out.png",
        "https://user@public.example/out.png",
        "https://public.example:444/out.png",
    ],
)
async def test_submit_rejects_unsafe_url_results_before_download(
    tmp_path: Path,
    unsafe_url: str,
) -> None:
    payload = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "fileData": {
                                "mimeType": "image/png",
                                "fileUri": unsafe_url,
                            }
                        }
                    ]
                }
            }
        ]
    }
    result_requests: list[httpx.Request] = []

    def unexpected_download(request: httpx.Request) -> httpx.Response:
        result_requests.append(request)
        return httpx.Response(500)

    result_downloader = SafeResultDownloader(
        transport=httpx.MockTransport(unexpected_download),
        resolver=_public_result_resolver,
        max_bytes=1024,
    )
    try:
        with pytest.raises(AgentError) as caught:
            await _submit_payload(
                tmp_path,
                payload,
                result_downloader=result_downloader,
            )
    finally:
        await result_downloader.aclose()

    assert caught.value.detail.category == ErrorCategory.PROVIDER_TERMINAL
    assert caught.value.detail.retryable is False
    assert result_requests == []


@pytest.mark.asyncio
async def test_poll_rejects_nonterminal_submission_without_http(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []
    async with _recording_client(requests) as client:
        generator = ChiyunImageGenerator(
            client,
            base_url="https://fictional-chiyun.test",
            api_key="fixture-key",
            model="fictional-model",
            staging_dir=tmp_path / "staging",
            result_downloader=INLINE_FIXTURE_DOWNLOADER,
        )
        with pytest.raises(AgentError) as caught:
            await generator.poll(
                ProviderSubmission(
                    provider="chiyun",
                    provider_task_id="fictional-pending-id",
                    status="pending",
                )
            )

    assert caught.value.detail.category == ErrorCategory.VALIDATION
    assert caught.value.detail.retryable is False
    assert requests == []


@pytest.mark.asyncio
async def test_submit_rejects_invalid_json_without_raw_response(
    tmp_path: Path,
) -> None:
    raw = "fictional-raw-invalid-json-secret"

    def invalid_json(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=raw)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(invalid_json)
    ) as client:
        generator = ChiyunImageGenerator(
            client,
            base_url="https://fictional-chiyun.test",
            api_key="fixture-key",
            model="fictional-model",
            staging_dir=tmp_path / "staging",
            result_downloader=INLINE_FIXTURE_DOWNLOADER,
        )
        with pytest.raises(AgentError) as caught:
            await generator.submit(
                _task(output_count=1),
                [_asset(tmp_path, "asset-blue"), _asset(tmp_path, "asset-green")],
            )

    assert caught.value.detail.category == ErrorCategory.PROVIDER_TERMINAL
    assert raw not in str(caught.value.detail)


@pytest.mark.asyncio
async def test_probe_models_only_reads_model_list_and_parses_ids(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def models(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "models": [
                    {"name": "models/fictional-model"},
                    {"name": "models/another-model"},
                    {"name": "models/fictional-model"},
                ]
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(models)
    ) as client:
        generator = ChiyunImageGenerator(
            client,
            base_url="https://fictional-chiyun.test",
            api_key="fixture-key",
            model="fictional-model",
            staging_dir=tmp_path / "staging",
            result_downloader=INLINE_FIXTURE_DOWNLOADER,
        )
        result = await generator.probe_models()

    assert result.status == "available"
    assert result.model_ids == ["fictional-model", "another-model"]
    assert result.configured_model_available is True
    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", "/v1beta/models")
    ]
    assert requests[0].headers["authorization"] == "Bearer fixture-key"
    assert all("generateContent" not in request.url.path for request in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [404, 405, 501])
async def test_probe_models_reports_no_free_verification_when_unsupported(
    tmp_path: Path,
    status_code: int,
) -> None:
    requests: list[httpx.Request] = []

    def unsupported(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status_code)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(unsupported)
    ) as client:
        generator = ChiyunImageGenerator(
            client,
            base_url="https://fictional-chiyun.test",
            api_key="fixture-key",
            model="fictional-model",
            staging_dir=tmp_path / "staging",
            result_downloader=INLINE_FIXTURE_DOWNLOADER,
        )
        result = await generator.probe_models()

    assert result.status == "unsupported"
    assert result.model_ids == []
    assert result.configured_model_available is None
    assert "无法无费用验证模型" in result.message
    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", "/v1beta/models")
    ]


@pytest.mark.asyncio
async def test_probe_models_treats_missing_models_structure_as_unsupported(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def missing_structure(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"fictional": []})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(missing_structure)
    ) as client:
        generator = ChiyunImageGenerator(
            client,
            base_url="https://fictional-chiyun.test",
            api_key="fixture-key",
            model="fictional-model",
            staging_dir=tmp_path / "staging",
            result_downloader=INLINE_FIXTURE_DOWNLOADER,
        )
        result = await generator.probe_models()

    assert result.status == "unsupported"
    assert "无法无费用验证模型" in result.message
    assert len(requests) == 1
    assert requests[0].method == "GET"


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [429, 503])
async def test_probe_models_classifies_retryable_http_errors(
    tmp_path: Path,
    status_code: int,
) -> None:
    requests: list[httpx.Request] = []

    def unavailable(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status_code)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(unavailable)
    ) as client:
        generator = ChiyunImageGenerator(
            client,
            base_url="https://fictional-chiyun.test",
            api_key="fixture-key",
            model="fictional-model",
            staging_dir=tmp_path / "staging",
            result_downloader=INLINE_FIXTURE_DOWNLOADER,
        )
        with pytest.raises(AgentError) as caught:
            await generator.probe_models()

    assert caught.value.detail.category == ErrorCategory.TRANSIENT
    assert caught.value.detail.retryable is True
    assert len(requests) == 1
    assert requests[0].method == "GET"


@pytest.mark.asyncio
async def test_probe_models_classifies_connection_error_as_retryable(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def fail(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise httpx.ConnectError("fictional connection failure", request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(fail)
    ) as client:
        generator = ChiyunImageGenerator(
            client,
            base_url="https://fictional-chiyun.test",
            api_key="fixture-key",
            model="fictional-model",
            staging_dir=tmp_path / "staging",
            result_downloader=INLINE_FIXTURE_DOWNLOADER,
        )
        with pytest.raises(AgentError) as caught:
            await generator.probe_models()

    assert caught.value.detail.category == ErrorCategory.TRANSIENT
    assert caught.value.detail.retryable is True
    assert len(requests) == 1
    assert requests[0].method == "GET"
