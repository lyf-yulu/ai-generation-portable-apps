import asyncio
import base64
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from feishu_generation_agent.config import Settings
from feishu_generation_agent.domain.document import RequirementRequest, SourceType
from feishu_generation_agent.domain.errors import AgentError, ErrorCategory
from feishu_generation_agent.integrations.feishu_client import FeishuClient
from feishu_generation_agent.integrations.feishu_source import (
    FeishuDocumentSource,
    parse_feishu_url,
)
from feishu_generation_agent.storage.files import FileStore


FIXTURES_DIR = Path(__file__).parents[1] / "fixtures"


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES_DIR / name).read_text("utf-8"))


class FakeFeishuClient:
    def __init__(self, blocks: list[dict[str, Any]], media: bytes) -> None:
        self.blocks = blocks
        self.media = media
        self.download_calls: list[str] = []
        self.wiki_type = "docx"
        self.download_error: Exception | None = None

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
    ) -> dict:
        del method, json_body
        if path == "/open-apis/wiki/v2/spaces/get_node":
            assert params == {"token": "wikcn456"}
            return {
                "code": 0,
                "data": {
                    "node": {
                        "obj_type": self.wiki_type,
                        "obj_token": "doccn-from-wiki",
                    }
                },
            }
        document_id = path.rsplit("/", 1)[-1]
        return {
            "code": 0,
            "data": {
                "document": {
                    "document_id": document_id,
                    "title": "虚构纸船需求",
                    "revision_id": 17,
                }
            },
        }

    async def iter_items(
        self, path: str, *, params: dict | None = None
    ) -> list[dict]:
        assert path.endswith("/blocks")
        assert params is None
        return self.blocks

    async def download_media(self, file_token: str) -> tuple[bytes, str]:
        self.download_calls.append(file_token)
        if self.download_error is not None:
            raise self.download_error
        return self.media, "image/png"


@pytest.fixture
def file_store(tmp_path: Path) -> FileStore:
    return FileStore(
        tmp_path / "data", tmp_path / "outputs", max_bytes=1024 * 1024
    )


def test_parse_docx_and_wiki_links_and_ignore_query_fragment():
    assert parse_feishu_url("https://acme.feishu.cn/docx/doccn123") == (
        SourceType.DOCX,
        "doccn123",
    )
    assert parse_feishu_url(
        "https://fiction.larksuite.com/wiki/wikcn456?from=space#heading"
    ) == (SourceType.WIKI, "wikcn456")

    with pytest.raises(ValueError, match="只支持 docx 或 wiki"):
        parse_feishu_url("https://acme.feishu.cn/sheets/sht123")
    for invalid_url in (
        "http://acme.feishu.cn/docx/doccn123",
        "https://example.com/docx/doccn123",
        "https://acme.feishu.cn/docx/",
    ):
        with pytest.raises(ValueError):
            parse_feishu_url(invalid_url)


async def test_client_caches_tenant_token_and_guards_concurrent_refresh():
    token_requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_requests
        assert request.url.path == "/open-apis/auth/v3/tenant_access_token/internal"
        token_requests += 1
        await asyncio.sleep(0.01)
        return httpx.Response(
            200,
            json={
                "code": 0,
                "tenant_access_token": "fiction-tenant-token",
                "expire": 7200,
            },
        )

    async with httpx.AsyncClient(
        base_url="https://open.feishu.cn",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = FeishuClient(
            Settings(lark_app_id="fiction-app", lark_app_secret="fiction-secret"),
            http_client=http_client,
        )
        tokens = await asyncio.gather(*(client.tenant_token() for _ in range(5)))

    assert tokens == ["fiction-tenant-token"] * 5
    assert token_requests == 1


@pytest.mark.parametrize("auth_failure", ["http-401", "feishu-code"])
async def test_request_json_refreshes_token_exactly_once(auth_failure: str):
    token_requests = 0
    api_requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_requests, api_requests
        if request.url.path.endswith("tenant_access_token/internal"):
            token_requests += 1
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "tenant_access_token": f"fiction-token-{token_requests}",
                    "expire": 7200,
                },
            )
        api_requests += 1
        if api_requests == 1:
            if auth_failure == "http-401":
                return httpx.Response(401, json={"code": 99991663, "msg": "expired"})
            return httpx.Response(200, json={"code": 99991663, "msg": "expired"})
        assert request.headers["Authorization"] == "Bearer fiction-token-2"
        return httpx.Response(200, json={"code": 0, "data": {"ok": True}})

    async with httpx.AsyncClient(
        base_url="https://open.feishu.cn",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = FeishuClient(
            Settings(lark_app_id="fiction-app", lark_app_secret="fiction-secret"),
            http_client=http_client,
        )
        result = await client.request_json("GET", "/open-apis/docx/v1/test")

    assert result["data"]["ok"] is True
    assert token_requests == 2
    assert api_requests == 2


async def test_request_json_does_not_refresh_twice_and_maps_errors():
    responses = [
        httpx.Response(
            200,
            json={"code": 0, "tenant_access_token": "token-1", "expire": 7200},
        ),
        httpx.Response(401, json={"code": 99991663, "msg": "expired"}),
        httpx.Response(
            200,
            json={"code": 0, "tenant_access_token": "token-2", "expire": 7200},
        ),
        httpx.Response(401, json={"code": 99991663, "msg": "expired again"}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return responses.pop(0)

    async with httpx.AsyncClient(
        base_url="https://open.feishu.cn",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = FeishuClient(
            Settings(lark_app_id="fiction-app", lark_app_secret="fiction-secret"),
            http_client=http_client,
        )
        with pytest.raises(AgentError) as raised:
            await client.request_json("GET", "/open-apis/docx/v1/test")

    assert raised.value.detail.category == ErrorCategory.PERMISSION
    assert raised.value.detail.retryable is False
    assert responses == []


@pytest.mark.parametrize(
    ("status", "payload", "category", "retryable"),
    [
        (403, {"code": 0}, ErrorCategory.PERMISSION, False),
        (200, {"code": 99991672, "msg": "forbidden"}, ErrorCategory.PERMISSION, False),
        (429, {"code": 1, "msg": "busy"}, ErrorCategory.TRANSIENT, True),
        (503, {"code": 1, "msg": "down"}, ErrorCategory.TRANSIENT, True),
        (400, {"code": 1770001, "msg": "invalid"}, ErrorCategory.DOCUMENT, False),
    ],
)
async def test_request_json_maps_api_failures(
    status: int,
    payload: dict,
    category: ErrorCategory,
    retryable: bool,
):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "token", "expire": 7200},
            )
        return httpx.Response(status, json=payload)

    async with httpx.AsyncClient(
        base_url="https://open.feishu.cn",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = FeishuClient(
            Settings(lark_app_id="fiction-app", lark_app_secret="fiction-secret"),
            http_client=http_client,
        )
        with pytest.raises(AgentError) as raised:
            await client.request_json("GET", "/open-apis/docx/v1/test")

    assert raised.value.detail.category == category
    assert raised.value.detail.retryable is retryable
    assert raised.value.detail.message
    assert raised.value.detail.technical_detail


@pytest.mark.parametrize(
    ("status", "category", "retryable"),
    [
        (403, ErrorCategory.PERMISSION, False),
        (429, ErrorCategory.TRANSIENT, True),
        (503, ErrorCategory.TRANSIENT, True),
    ],
)
async def test_request_json_maps_non_json_http_failures(
    status: int,
    category: ErrorCategory,
    retryable: bool,
):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "token", "expire": 7200},
            )
        return httpx.Response(status, text="fictional upstream failure")

    async with httpx.AsyncClient(
        base_url="https://open.feishu.cn",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = FeishuClient(
            Settings(lark_app_id="fiction-app", lark_app_secret="fiction-secret"),
            http_client=http_client,
        )
        with pytest.raises(AgentError) as raised:
            await client.request_json("GET", "/open-apis/docx/v1/test")

    assert raised.value.detail.category == category
    assert raised.value.detail.retryable is retryable


async def test_iter_items_follows_pagination_and_rejects_repeated_token():
    page_requests: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "token", "expire": 7200},
            )
        token = request.url.params.get("page_token")
        page_requests.append(token)
        if token is None:
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {"items": [{"block_id": "one"}], "has_more": True, "page_token": "next"},
                },
            )
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {"items": [{"block_id": "two"}], "has_more": False},
            },
        )

    async with httpx.AsyncClient(
        base_url="https://open.feishu.cn",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = FeishuClient(
            Settings(lark_app_id="fiction-app", lark_app_secret="fiction-secret"),
            http_client=http_client,
        )
        items = await client.iter_items("/open-apis/docx/v1/documents/doc/blocks")

    assert [item["block_id"] for item in items] == ["one", "two"]
    assert page_requests == [None, "next"]

    repeated_responses = [
        {"code": 0, "data": {"items": [], "has_more": True, "page_token": "same"}},
        {"code": 0, "data": {"items": [], "has_more": True, "page_token": "same"}},
    ]

    async def fake_request_json(*args: Any, **kwargs: Any) -> dict:
        del args, kwargs
        return repeated_responses.pop(0)

    client.request_json = fake_request_json  # type: ignore[method-assign]
    with pytest.raises(AgentError) as raised:
        await client.iter_items("/blocks")
    assert raised.value.detail.category == ErrorCategory.DOCUMENT


async def test_download_media_returns_response_content_type():
    png = base64.b64decode(_fixture("feishu_docx_blocks.json")["media_base64"])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "token", "expire": 7200},
            )
        return httpx.Response(
            200, content=png, headers={"Content-Type": "image/png; charset=binary"}
        )

    async with httpx.AsyncClient(
        base_url="https://open.feishu.cn",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = FeishuClient(
            Settings(lark_app_id="fiction-app", lark_app_secret="fiction-secret"),
            http_client=http_client,
        )
        content, mime_type = await client.download_media("fiction-file-token")

    assert content == png
    assert mime_type == "image/png; charset=binary"


async def test_download_media_refreshes_token_for_json_api_error_then_succeeds():
    png = base64.b64decode(_fixture("feishu_docx_blocks.json")["media_base64"])
    token_requests = 0
    media_requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_requests, media_requests
        if request.url.path.endswith("tenant_access_token/internal"):
            token_requests += 1
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "tenant_access_token": f"fiction-token-{token_requests}",
                    "expire": 7200,
                },
            )
        media_requests += 1
        if media_requests == 1:
            return httpx.Response(200, json={"code": 99991663, "msg": "expired"})
        assert request.headers["Authorization"] == "Bearer fiction-token-2"
        return httpx.Response(200, content=png, headers={"Content-Type": "image/png"})

    async with httpx.AsyncClient(
        base_url="https://open.feishu.cn",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = FeishuClient(
            Settings(lark_app_id="fiction-app", lark_app_secret="fiction-secret"),
            http_client=http_client,
        )
        content, mime_type = await client.download_media("fiction-file-token")

    assert content == png
    assert mime_type == "image/png"
    assert token_requests == 2
    assert media_requests == 2


async def test_download_media_refreshes_only_once_for_repeated_token_error():
    token_requests = 0
    media_requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_requests, media_requests
        if request.url.path.endswith("tenant_access_token/internal"):
            token_requests += 1
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "tenant_access_token": f"fiction-token-{token_requests}",
                    "expire": 7200,
                },
            )
        media_requests += 1
        return httpx.Response(200, json={"code": 99991663, "msg": "expired"})

    async with httpx.AsyncClient(
        base_url="https://open.feishu.cn",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = FeishuClient(
            Settings(lark_app_id="fiction-app", lark_app_secret="fiction-secret"),
            http_client=http_client,
        )
        with pytest.raises(AgentError) as raised:
            await client.download_media("fiction-file-token")

    assert raised.value.detail.category == ErrorCategory.PERMISSION
    assert raised.value.detail.retryable is False
    assert token_requests == 2
    assert media_requests == 2


async def test_download_media_rejects_parseable_nonzero_code_as_document_error():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "token", "expire": 7200},
            )
        return httpx.Response(
            200,
            content=b'{"code":1770001,"msg":"invalid media"}',
            headers={"Content-Type": "image/png"},
        )

    async with httpx.AsyncClient(
        base_url="https://open.feishu.cn",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = FeishuClient(
            Settings(lark_app_id="fiction-app", lark_app_secret="fiction-secret"),
            http_client=http_client,
        )
        with pytest.raises(AgentError) as raised:
            await client.download_media("fiction-file-token")

    assert raised.value.detail.category == ErrorCategory.DOCUMENT
    assert raised.value.detail.retryable is False


async def test_ingest_docx_preserves_hierarchy_and_stable_references(
    file_store: FileStore,
):
    fixture = _fixture("feishu_docx_blocks.json")
    media = base64.b64decode(fixture["media_base64"])
    client = FakeFeishuClient(fixture["items"], media)
    source = FeishuDocumentSource(client, file_store)

    document = await source.ingest(
        RequirementRequest(source_url="https://fiction.feishu.cn/docx/doccn123")
    )

    assert document.document_id == "doccn123"
    assert document.source_type == SourceType.DOCX
    assert document.source_token == "doccn123"
    assert document.revision == 17
    assert [block.block_id for block in document.blocks] == [
        "fiction-page",
        "fiction-paragraph",
        "fiction-image",
    ]
    assert document.blocks[1].path == ["fiction-page", "fiction-paragraph"]
    assert "[block:fiction-paragraph]" in document.text_view
    assert "[image:image-1]" in document.text_view
    assert document.blocks[2].image_asset_id == "image-1"
    assert document.media_assets[0].local_path.parts[-3:-1] == (
        "doccn123",
        "inputs",
    )


async def test_ingest_accepts_feishu_empty_root_parent_and_null_leaf_children(
    file_store: FileStore,
):
    blocks = [
        {
            "block_id": "doccn123",
            "block_type": 1,
            "parent_id": "",
            "children": ["paragraph"],
        },
        {
            "block_id": "paragraph",
            "block_type": 2,
            "parent_id": "doccn123",
            "children": None,
            "text": {"elements": []},
        },
    ]
    source = FeishuDocumentSource(FakeFeishuClient(blocks, b""), file_store)

    document = await source.ingest(
        RequirementRequest(source_url="https://fiction.feishu.cn/docx/doccn123")
    )

    assert [block.block_id for block in document.blocks] == [
        "doccn123",
        "paragraph",
    ]
    assert document.blocks[1].path == ["doccn123", "paragraph"]


async def test_wiki_resolution_requires_docx_and_get_revision(file_store: FileStore):
    fixture = _fixture("feishu_docx_blocks.json")
    client = FakeFeishuClient(
        fixture["items"], base64.b64decode(fixture["media_base64"])
    )
    source = FeishuDocumentSource(client, file_store)

    document = await source.ingest(
        RequirementRequest(source_url="https://fiction.feishu.cn/wiki/wikcn456")
    )

    assert document.document_id == "doccn-from-wiki"
    assert document.source_type == SourceType.WIKI
    assert document.source_token == "wikcn456"
    assert await source.get_revision(
        "https://fiction.feishu.cn/wiki/wikcn456"
    ) == 17

    client.wiki_type = "sheet"
    with pytest.raises(AgentError) as raised:
        await source.ingest(
            RequirementRequest(source_url="https://fiction.feishu.cn/wiki/wikcn456")
        )
    assert raised.value.detail.category == ErrorCategory.DOCUMENT


async def test_ingest_table_uses_row_major_dfs_and_caches_shared_image(
    file_store: FileStore,
):
    fixture = _fixture("feishu_storyboard_blocks.json")
    client = FakeFeishuClient(
        fixture["items"], base64.b64decode(fixture["media_base64"])
    )
    source = FeishuDocumentSource(client, file_store)

    document = await source.ingest(
        RequirementRequest(source_url="https://fiction.feishu.cn/docx/doccn123")
    )

    assert [block.block_id for block in document.blocks] == [
        "story-page",
        "story-intro",
        "story-table",
        "cell-00",
        "shot-1",
        "cell-01",
        "image-block-1",
        "cell-10",
        "shot-2",
        "cell-11",
        "image-block-2",
    ]
    cells = [block for block in document.blocks if block.table_row is not None]
    assert [(block.table_row, block.table_column) for block in cells] == [
        (0, 0),
        (0, 1),
        (1, 0),
        (1, 1),
    ]
    assert [asset.source_block_id for asset in document.media_assets] == [
        "image-block-1",
        "image-block-2",
    ]
    assert [asset.asset_id for asset in document.media_assets] == [
        "image-1",
        "image-2",
    ]
    assert document.text_view.index("[image:image-1]") < document.text_view.index(
        "[image:image-2]"
    )
    assert client.download_calls == ["fiction-shared-image-token"]
    assert document.media_assets[0].local_path == document.media_assets[1].local_path


@pytest.mark.parametrize(
    "blocks",
    [
        [
            {"block_id": "root", "block_type": 1, "children": ["missing"]},
        ],
        [
            {
                "block_id": "root",
                "block_type": 1,
                "children": ["child", "child"],
            },
            {"block_id": "child", "parent_id": "root", "block_type": 2},
        ],
        [
            {
                "block_id": "root",
                "block_type": 1,
                "children": ["parent-a", "parent-b"],
            },
            {
                "block_id": "parent-a",
                "parent_id": "root",
                "block_type": 2,
                "children": ["child"],
            },
            {
                "block_id": "parent-b",
                "parent_id": "root",
                "block_type": 2,
                "children": ["child"],
            },
            {"block_id": "child", "parent_id": "parent-a", "block_type": 2},
        ],
        [
            {
                "block_id": "root",
                "block_type": 1,
                "children": ["parent-a", "parent-b"],
            },
            {
                "block_id": "parent-a",
                "parent_id": "root",
                "block_type": 2,
                "children": ["child"],
            },
            {"block_id": "parent-b", "parent_id": "root", "block_type": 2},
            {"block_id": "child", "parent_id": "parent-b", "block_type": 2},
        ],
        [
            {"block_id": "orphan", "parent_id": "missing", "block_type": 2},
        ],
    ],
    ids=[
        "missing-child",
        "duplicate-child",
        "multi-parent-child",
        "declared-parent-mismatch",
        "missing-declared-parent",
    ],
)
async def test_ingest_rejects_inconsistent_block_references(
    blocks: list[dict[str, Any]],
    file_store: FileStore,
):
    source = FeishuDocumentSource(FakeFeishuClient(blocks, b""), file_store)

    with pytest.raises(AgentError) as raised:
        await source.ingest(
            RequirementRequest(source_url="https://fiction.feishu.cn/docx/doccn123")
        )

    assert raised.value.detail.category == ErrorCategory.DOCUMENT
    assert raised.value.detail.retryable is False


@pytest.mark.parametrize(
    ("children", "cells", "cell_ids"),
    [
        ([], ["cell", "cell"], ["cell"]),
        (
            ["cell-b", "cell-a"],
            ["cell-a", "cell-b"],
            ["cell-a", "cell-b"],
        ),
    ],
    ids=["duplicate-table-cell", "table-children-mismatch"],
)
async def test_ingest_rejects_inconsistent_table_references(
    children: list[str],
    cells: list[str],
    cell_ids: list[str],
    file_store: FileStore,
):
    blocks: list[dict[str, Any]] = [
        {
            "block_id": "table",
            "block_type": 31,
            "children": children,
            "table": {
                "cells": cells,
                "property": {"row_size": 1, "column_size": 2},
            },
        },
        *[
            {"block_id": cell_id, "parent_id": "table", "block_type": 32}
            for cell_id in cell_ids
        ],
    ]
    source = FeishuDocumentSource(FakeFeishuClient(blocks, b""), file_store)

    with pytest.raises(AgentError) as raised:
        await source.ingest(
            RequirementRequest(source_url="https://fiction.feishu.cn/docx/doccn123")
        )

    assert raised.value.detail.category == ErrorCategory.DOCUMENT
    assert raised.value.detail.retryable is False


async def test_image_download_failure_is_blocking_and_never_silently_skipped(
    file_store: FileStore,
):
    fixture = _fixture("feishu_docx_blocks.json")
    client = FakeFeishuClient(
        fixture["items"], base64.b64decode(fixture["media_base64"])
    )
    client.download_error = RuntimeError("fictional download failure")
    source = FeishuDocumentSource(client, file_store)

    document = await source.ingest(
        RequirementRequest(source_url="https://fiction.feishu.cn/docx/doccn123")
    )

    assert len(document.media_assets) == 1
    asset = document.media_assets[0]
    assert asset.asset_id == "image-1"
    assert asset.file_token == "fiction-file-token"
    assert asset.size == 0
    assert asset.sha256 == ""
    assert asset.mime_type == "application/octet-stream"
    assert asset.download_error == "fictional download failure"
    assert asset.local_path == Path("__missing__") / "doccn123" / "image-1.missing"
    assert not asset.local_path.exists()
    assert any("阻塞" in issue and "image-1" in issue for issue in document.ingest_issues)
