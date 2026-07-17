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


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _jpeg_fixture() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (1, 1), "green").save(buffer, format="JPEG")
    return buffer.getvalue()


JPEG_1X1 = _jpeg_fixture()


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
    **generator_options: Any,
):
    requests: list[httpx.Request] = []

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
        submission = await generator.submit(
            _task(output_count=1),
            [_asset(tmp_path, "asset-blue"), _asset(tmp_path, "asset-green")],
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
    assert submission.provider_task_id == "fictional-response-001"
    assert submission.status == "succeeded"
    assert submission.result_items[0].mime_type == "image/png"
    assert submission.result_items[0].base64_data == (
        "ZmljdGlvbmFsLWltYWdlLXJlc3VsdA=="
    )


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
    updates: dict[str, str],
    field_name: str,
) -> None:
    values = {
        "base_url": "https://fictional-chiyun.test",
        "api_key": "fixture-key-never-sent-externally",
        "model": "fictional-model",
    }
    values.update(updates)

    with pytest.raises(AgentError) as caught:
        ChiyunImageGenerator(httpx.AsyncClient(), **values)

    assert caught.value.detail.category == ErrorCategory.CONFIGURATION
    assert caught.value.detail.retryable is False
    assert field_name in caught.value.detail.technical_detail
    if values["api_key"].strip():
        assert values["api_key"] not in str(caught.value.detail)


def test_constructor_keeps_api_key_secret_and_out_of_repr() -> None:
    api_key = "fixture-secret-key-for-repr-test"
    generator = ChiyunImageGenerator(
        httpx.AsyncClient(),
        base_url="https://fictional-chiyun.test",
        api_key=SecretStr(api_key),
        model="fictional-model",
    )

    assert api_key not in repr(generator)
    assert api_key not in repr(vars(generator))


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

    submission, _ = await _submit_payload(tmp_path, payload)

    assert [item.mime_type for item in submission.result_items] == [
        "image/webp",
        "image/jpeg",
    ]
    assert submission.result_items[0].base64_data == snake_data
    assert submission.result_items[1].url == (
        "https://cdn.fictional.test/result.jpg?sig=fake"
    )


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
        )
        submission = await generator.submit(
            _task(output_count=1),
            [_asset(tmp_path, "asset-blue"), _asset(tmp_path, "asset-green")],
        )
        polled = await generator.poll(submission)

    assert polled is submission
    assert len(requests) == 1
    assert "result_count=1" in caplog.text
    assert "image/png" in caplog.text
    assert key not in caplog.text
    assert encoded not in caplog.text


@pytest.mark.asyncio
async def test_poll_rejects_nonterminal_submission_without_http() -> None:
    requests: list[httpx.Request] = []
    async with _recording_client(requests) as client:
        generator = ChiyunImageGenerator(
            client,
            base_url="https://fictional-chiyun.test",
            api_key="fixture-key",
            model="fictional-model",
        )
        with pytest.raises(AgentError) as caught:
            await generator.poll(
                ProviderSubmission(
                    provider="chiyun",
                    provider_task_id="fictional-pending-id",
                    status="submitted",
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
        )
        with pytest.raises(AgentError) as caught:
            await generator.submit(
                _task(output_count=1),
                [_asset(tmp_path, "asset-blue"), _asset(tmp_path, "asset-green")],
            )

    assert caught.value.detail.category == ErrorCategory.PROVIDER_TERMINAL
    assert raw not in str(caught.value.detail)


@pytest.mark.asyncio
async def test_probe_models_only_reads_model_list_and_parses_ids() -> None:
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
async def test_probe_models_treats_missing_models_structure_as_unsupported() -> None:
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
        )
        result = await generator.probe_models()

    assert result.status == "unsupported"
    assert "无法无费用验证模型" in result.message
    assert len(requests) == 1
    assert requests[0].method == "GET"


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [429, 503])
async def test_probe_models_classifies_retryable_http_errors(
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
        )
        with pytest.raises(AgentError) as caught:
            await generator.probe_models()

    assert caught.value.detail.category == ErrorCategory.TRANSIENT
    assert caught.value.detail.retryable is True
    assert len(requests) == 1
    assert requests[0].method == "GET"


@pytest.mark.asyncio
async def test_probe_models_classifies_connection_error_as_retryable() -> None:
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
        )
        with pytest.raises(AgentError) as caught:
            await generator.probe_models()

    assert caught.value.detail.category == ErrorCategory.TRANSIENT
    assert caught.value.detail.retryable is True
    assert len(requests) == 1
    assert requests[0].method == "GET"
