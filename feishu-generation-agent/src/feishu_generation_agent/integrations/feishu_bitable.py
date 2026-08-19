from collections.abc import Mapping
from typing import Any, Protocol

from pydantic import BaseModel

from feishu_generation_agent.domain.bitable import (
    BitableLocation,
    BitableTaskSummary,
)
from feishu_generation_agent.integrations.bitable_url import (
    parse_requirement_source,
)


_EXPECTED_FIELDS: dict[str, frozenset[int] | None] = {
    # The primary display column may be text, auto-number, number, or another
    # displayable Bitable type. It is never used as record identity.
    "文本": None,
    "需求来源": frozenset({15}),
    "执行人": frozenset({11}),
    "结果": frozenset({17}),
}


class BitableSchemaError(ValueError):
    """The configured table does not expose the required read-only schema."""


class BitableSchema(BaseModel):
    title_field_id: str
    source_field_id: str
    executor_field_id: str
    result_field_id: str


class FeishuJsonClient(Protocol):
    async def request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
    ) -> dict: ...

    async def iter_items(
        self, path: str, *, params: dict | None = None
    ) -> list[dict]: ...

    async def upload_media_all(
        self,
        filename: str,
        content: bytes,
        mime_type: str,
        *,
        parent_type: str,
        parent_node: str,
    ) -> str: ...

    async def prepare_media_upload(
        self,
        filename: str,
        size: int,
        *,
        parent_type: str,
        parent_node: str,
    ) -> tuple[str, int]: ...

    async def upload_media_part(
        self, upload_id: str, sequence: int, content: bytes
    ) -> None: ...

    async def finish_media_upload(self, upload_id: str, block_count: int) -> str: ...


class FeishuBitableClient:
    def __init__(self, client: FeishuJsonClient) -> None:
        self._client = client

    async def resolve_location(self, location: BitableLocation) -> BitableLocation:
        payload = await self._client.request_json(
            "GET",
            "/open-apis/wiki/v2/spaces/get_node",
            params={"token": location.wiki_token},
        )
        node = payload.get("data", {}).get("node", {})
        app_token = node.get("obj_token")
        if (
            node.get("obj_type") != "bitable"
            or not isinstance(app_token, str)
            or not app_token
        ):
            raise BitableSchemaError("配置的 wiki 节点不是多维表格")
        if location.app_token is not None and location.app_token != app_token:
            raise BitableSchemaError("多维表格 app_token 与 wiki 节点不一致")
        return location.model_copy(update={"app_token": app_token})

    async def ensure_schema(self, location: BitableLocation) -> BitableSchema:
        app_token = _app_token(location)
        items = await self._client.iter_items(
            f"/open-apis/bitable/v1/apps/{app_token}/tables/{location.table_id}/fields"
        )
        by_name: dict[str, dict] = {}
        for item in items:
            name = item.get("field_name")
            if isinstance(name, str):
                if name in _EXPECTED_FIELDS and name in by_name:
                    raise BitableSchemaError(f"多维表格存在重复字段：{name}")
                by_name[name] = item

        field_ids: dict[str, str] = {}
        for name, expected_types in _EXPECTED_FIELDS.items():
            field = by_name.get(name)
            if field is None:
                raise BitableSchemaError(f"多维表格缺少字段：{name}")
            if (
                expected_types is not None
                and field.get("type") not in expected_types
            ):
                raise BitableSchemaError(
                    f"多维表格字段类型不兼容：{name} 应为 "
                    f"{sorted(expected_types)}"
                )
            field_id = field.get("field_id")
            if not isinstance(field_id, str) or not field_id:
                raise BitableSchemaError(f"多维表格字段缺少 field_id：{name}")
            field_ids[name] = field_id

        return BitableSchema(
            title_field_id=field_ids["文本"],
            source_field_id=field_ids["需求来源"],
            executor_field_id=field_ids["执行人"],
            result_field_id=field_ids["结果"],
        )

    async def list_tasks(
        self, location: BitableLocation, schema: BitableSchema
    ) -> list[BitableTaskSummary]:
        # Validation happens in ensure_schema; records use stable field names.
        del schema
        app_token = _app_token(location)
        records = await self._client.iter_items(
            f"/open-apis/bitable/v1/apps/{app_token}/tables/{location.table_id}/records",
            params={"view_id": location.view_id},
        )
        tasks: list[BitableTaskSummary] = []
        for record in records:
            fields = record.get("fields")
            record_id = record.get("record_id")
            if not isinstance(fields, Mapping) or not isinstance(record_id, str):
                continue
            if _has_value(fields.get("结果")):
                continue
            try:
                source_url = parse_requirement_source(fields.get("需求来源"))
            except (TypeError, ValueError):
                continue
            tasks.append(
                BitableTaskSummary(
                    record_id=record_id,
                    display_text=_display_text(fields.get("文本")) or record_id,
                    source_url=source_url,
                    executor_open_ids=_executor_ids(fields.get("执行人")),
                    executor_names=_executor_names(fields.get("执行人")),
                    has_result=False,
                )
            )
        return tasks

    async def get_record(
        self, location: BitableLocation, record_id: str
    ) -> dict:
        app_token = _app_token(location)
        payload = await self._client.request_json(
            "GET",
            f"/open-apis/bitable/v1/apps/{app_token}/tables/"
            f"{location.table_id}/records/{record_id}",
        )
        record = payload.get("data", {}).get("record")
        if not isinstance(record, dict):
            raise ValueError("飞书多维表格记录响应无效")
        return record

    async def write_result_attachments(
        self,
        location: BitableLocation,
        record_id: str,
        file_tokens: list[str],
    ) -> dict:
        if not file_tokens or any(
            not isinstance(token, str) or not token for token in file_tokens
        ):
            raise ValueError("结果附件 token 无效")
        app_token = _app_token(location)
        payload = await self._client.request_json(
            "PUT",
            f"/open-apis/bitable/v1/apps/{app_token}/tables/"
            f"{location.table_id}/records/{record_id}",
            json_body={
                "fields": {
                    "结果": [{"file_token": token} for token in file_tokens]
                }
            },
        )
        record = payload.get("data", {}).get("record", {})
        return record if isinstance(record, dict) else {}

    async def upload_file_all(
        self,
        filename: str,
        content: bytes,
        mime_type: str,
        *,
        parent_type: str,
        parent_node: str,
    ) -> str:
        return await self._client.upload_media_all(
            filename,
            content,
            mime_type,
            parent_type=parent_type,
            parent_node=parent_node,
        )

    async def prepare_file_upload(
        self,
        filename: str,
        size: int,
        *,
        parent_type: str,
        parent_node: str,
    ) -> tuple[str, int]:
        return await self._client.prepare_media_upload(
            filename,
            size,
            parent_type=parent_type,
            parent_node=parent_node,
        )

    async def upload_file_part(
        self, upload_id: str, sequence: int, content: bytes
    ) -> None:
        await self._client.upload_media_part(upload_id, sequence, content)

    async def finish_file_upload(self, upload_id: str, block_count: int) -> str:
        return await self._client.finish_media_upload(upload_id, block_count)


def record_has_result(record: Mapping[str, Any]) -> bool:
    fields = record.get("fields")
    return isinstance(fields, Mapping) and _has_value(fields.get("结果"))


def _app_token(location: BitableLocation) -> str:
    if not isinstance(location.app_token, str) or not location.app_token:
        raise ValueError("多维表格位置尚未解析 app_token")
    return location.app_token


def _has_value(value: Any) -> bool:
    if value is None or value == "":
        return False
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def _display_text(value: Any) -> str:
    parts: list[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, (int, float)) and not isinstance(item, bool):
            parts.append(str(item))
        elif isinstance(item, Mapping):
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
            elif "value" in item:
                visit(item["value"])
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return "".join(parts).strip()


def _executor_ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        identity = item.get("open_id") or item.get("id") or item.get("user_id")
        if isinstance(identity, str) and identity and identity not in result:
            result.append(identity)
    return result


def _executor_names(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        name = item.get("name")
        if isinstance(name, str) and name.strip() and name.strip() not in result:
            result.append(name.strip())
    return result
