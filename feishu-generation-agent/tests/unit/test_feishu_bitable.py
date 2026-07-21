from copy import deepcopy

import httpx
import pytest

from feishu_generation_agent.config import Settings
from feishu_generation_agent.domain import BitableLocation
from feishu_generation_agent.integrations.feishu_bitable import (
    BitableSchemaError,
    FeishuBitableClient,
)
from feishu_generation_agent.integrations.feishu_client import FeishuClient


def _location() -> BitableLocation:
    return BitableLocation(
        wiki_token="wikiTABLE",
        table_id="tblTABLE",
        view_id="vewTASKS",
        source_url=(
            "https://tenant.feishu.cn/wiki/wikiTABLE"
            "?table=tblTABLE&view=vewTASKS"
        ),
    )


async def test_resolves_wiki_validates_schema_and_lists_eligible_view_records(
    fixture_json,
) -> None:
    fields = fixture_json("bitable_fields.json")["items"]
    records = fixture_json("bitable_records.json")["items"]
    requests: list[tuple[str, str, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(
            (request.method, request.url.path, dict(request.url.params.multi_items()))
        )
        path = request.url.path
        if path.endswith("tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "tenant_access_token": "fiction-token",
                    "expire": 7200,
                },
            )
        if path == "/open-apis/wiki/v2/spaces/get_node":
            assert request.url.params["token"] == "wikiTABLE"
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "node": {"obj_type": "bitable", "obj_token": "appTABLE"}
                    },
                },
            )
        if path.endswith("/fields"):
            if request.url.params.get("page_token") == "fields-next":
                return httpx.Response(
                    200,
                    json={"code": 0, "data": {"items": fields[2:], "has_more": False}},
                )
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "items": fields[:2],
                        "has_more": True,
                        "page_token": "fields-next",
                    },
                },
            )
        if path.endswith("/records"):
            assert request.url.params["view_id"] == "vewTASKS"
            if request.url.params.get("page_token") == "records-next":
                return httpx.Response(
                    200,
                    json={"code": 0, "data": {"items": records[4:], "has_more": False}},
                )
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "items": records[:4],
                        "has_more": True,
                        "page_token": "records-next",
                    },
                },
            )
        raise AssertionError(request.url)

    async with httpx.AsyncClient(
        base_url="https://open.feishu.cn",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        base_client = FeishuClient(
            Settings(lark_app_id="fiction-app", lark_app_secret="fiction-secret"),
            http_client=http_client,
        )
        client = FeishuBitableClient(base_client)
        resolved = await client.resolve_location(_location())
        schema = await client.ensure_schema(resolved)
        tasks = await client.list_tasks(resolved, schema)

    assert resolved.app_token == "appTABLE"
    assert schema.result_field_id == "fld_result"
    assert [task.record_id for task in tasks] == ["rec_valid", "rec_second_page"]
    assert tasks[0].display_text == "纸船短片"
    assert tasks[0].executor_open_ids == ["ou_alice", "ou_bob"]
    assert tasks[0].executor_names == ["Alice", "Bob"]
    assert tasks[1].executor_open_ids == ["ou_carol"]
    assert tasks[1].executor_names == ["Carol"]
    assert all(task.has_result is False for task in tasks)
    assert any(params.get("page_token") == "fields-next" for _, _, params in requests)
    assert any(params.get("page_token") == "records-next" for _, _, params in requests)


@pytest.mark.parametrize(
    ("field_name", "wrong_type"),
    [("需求来源", 1), ("执行人", 1), ("结果", 15)],
)
async def test_ensure_schema_rejects_incompatible_exact_field_types(
    fixture_json, field_name: str, wrong_type: int
) -> None:
    fields = deepcopy(fixture_json("bitable_fields.json")["items"])
    for field in fields:
        if field["field_name"] == field_name:
            field["type"] = wrong_type

    class FakeClient:
        async def iter_items(self, path: str, *, params=None):
            assert path.endswith("/fields")
            return fields

    client = FeishuBitableClient(FakeClient())
    with pytest.raises(BitableSchemaError, match=field_name):
        await client.ensure_schema(_location().model_copy(update={"app_token": "app"}))


async def test_primary_autonumber_title_is_accepted_and_displayed() -> None:
    fields = [
        {"field_id": "fld_title", "field_name": "文本", "type": 1005},
        {"field_id": "fld_source", "field_name": "需求来源", "type": 15},
        {"field_id": "fld_executor", "field_name": "执行人", "type": 11},
        {"field_id": "fld_result", "field_name": "结果", "type": 17},
    ]
    records = [
        {
            "record_id": "rec-numbered",
            "fields": {
                "文本": 1,
                "需求来源": "https://tenant.feishu.cn/docx/docNUMBERED",
                "执行人": [],
                "结果": [],
            },
        }
    ]

    class FakeClient:
        async def iter_items(self, path: str, *, params=None):
            return records if path.endswith("/records") else fields

    client = FeishuBitableClient(FakeClient())
    location = _location().model_copy(update={"app_token": "app"})
    schema = await client.ensure_schema(location)
    tasks = await client.list_tasks(location, schema)

    assert tasks[0].display_text == "1"


async def test_ensure_schema_rejects_duplicate_required_field_names(
    fixture_json,
) -> None:
    fields = deepcopy(fixture_json("bitable_fields.json")["items"])
    fields.append(
        {"field_id": "fld_duplicate", "field_name": "结果", "type": 17}
    )

    class FakeClient:
        async def iter_items(self, path: str, *, params=None):
            return fields

    client = FeishuBitableClient(FakeClient())
    with pytest.raises(BitableSchemaError, match="重复.*结果"):
        await client.ensure_schema(_location().model_copy(update={"app_token": "app"}))


async def test_get_record_refreshes_and_write_result_uses_attachment_payload() -> None:
    calls: list[tuple[str, str, dict | None]] = []

    class FakeClient:
        async def request_json(self, method: str, path: str, *, json_body=None, params=None):
            calls.append((method, path, json_body))
            if method == "GET":
                return {
                    "code": 0,
                    "data": {"record": {"record_id": "rec1", "fields": {"结果": []}}},
                }
            return {"code": 0, "data": {"record": {"record_id": "rec1"}}}

    client = FeishuBitableClient(FakeClient())
    location = _location().model_copy(update={"app_token": "appTABLE"})
    record = await client.get_record(location, "rec1")
    await client.write_result_attachments(location, "rec1", ["fileA", "fileB"])

    assert record["record_id"] == "rec1"
    assert calls == [
        (
            "GET",
            "/open-apis/bitable/v1/apps/appTABLE/tables/tblTABLE/records/rec1",
            None,
        ),
        (
            "PUT",
            "/open-apis/bitable/v1/apps/appTABLE/tables/tblTABLE/records/rec1",
            {"fields": {"结果": [{"file_token": "fileA"}, {"file_token": "fileB"}]}},
        ),
    ]


async def test_feishu_upload_accepts_bitable_parent_target() -> None:
    upload_body = b""
    upload_path = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal upload_body, upload_path
        if request.url.path.endswith("tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "tenant_access_token": "fiction-token",
                    "expire": 7200,
                },
            )
        if request.url.path.endswith("upload_all"):
            upload_path = request.url.path
            upload_body = request.content
            return httpx.Response(
                200, json={"code": 0, "data": {"file_token": "bitable-file"}}
            )
        raise AssertionError(request.url)

    async with httpx.AsyncClient(
        base_url="https://open.feishu.cn",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = FeishuBitableClient(
            FeishuClient(
                Settings(
                    lark_app_id="fiction-app",
                    lark_app_secret="fiction-secret",
                ),
                http_client=http_client,
            )
        )
        token = await client.upload_file_all(
            "result.png",
            b"image",
            "image/png",
            parent_type="bitable_file",
            parent_node="appTABLE",
        )

    assert token == "bitable-file"
    assert upload_path == "/open-apis/drive/v1/medias/upload_all"
    assert b"bitable_file" in upload_body
    assert b"appTABLE" in upload_body


async def test_feishu_bitable_chunked_upload_uses_media_api_paths() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "tenant_access_token": "fiction-token",
                    "expire": 7200,
                },
            )
        if request.url.path.endswith("upload_prepare"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {"upload_id": "upload-1", "block_size": 4},
                },
            )
        if request.url.path.endswith("upload_part"):
            return httpx.Response(200, json={"code": 0, "data": {}})
        if request.url.path.endswith("upload_finish"):
            return httpx.Response(
                200,
                json={"code": 0, "data": {"file_token": "bitable-file"}},
            )
        raise AssertionError(request.url)

    async with httpx.AsyncClient(
        base_url="https://open.feishu.cn",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = FeishuBitableClient(
            FeishuClient(
                Settings(
                    lark_app_id="fiction-app",
                    lark_app_secret="fiction-secret",
                ),
                http_client=http_client,
            )
        )
        assert await client.prepare_file_upload(
            "result.mp4",
            8,
            parent_type="bitable_file",
            parent_node="appTABLE",
        ) == ("upload-1", 4)
        await client.upload_file_part("upload-1", 0, b"1234")
        assert await client.finish_file_upload("upload-1", 2) == "bitable-file"

    assert "/open-apis/drive/v1/medias/upload_prepare" in paths
    assert "/open-apis/drive/v1/medias/upload_part" in paths
    assert "/open-apis/drive/v1/medias/upload_finish" in paths
