from collections.abc import Mapping
from typing import Any

from feishu_generation_agent.domain.bitable import BitableLocation
from feishu_generation_agent.domain.production_bitable import (
    ProductionSchema,
    ProductionSourceSnapshot,
    ProductionTaskSummary,
)
from feishu_generation_agent.integrations.bitable_url import parse_requirement_source
from feishu_generation_agent.integrations.feishu_bitable import BitableSchemaError


_REQUIRED_FIELDS: dict[str, frozenset[int]] = {
    "需求名称": frozenset({1}),
    "需求类型": frozenset({3}),
    "需求附件": frozenset({1, 15}),
    "项目名称": frozenset({4}),
    "发起人": frozenset({11}),
    "需求制作人": frozenset({11}),
    "当前进度": frozenset({3}),
}
_COMPLETED_PROGRESS = "已确认完成"


class ProductionBitableClient:
    def __init__(self, client: Any) -> None:
        self._client = client

    async def resolve_location(self, location: BitableLocation) -> BitableLocation:
        payload = await self._client.request_json(
            "GET",
            "/open-apis/wiki/v2/spaces/get_node",
            params={"token": location.wiki_token},
        )
        node = payload.get("data", {}).get("node", {})
        app_token = node.get("obj_token")
        if node.get("obj_type") != "bitable" or not isinstance(app_token, str) or not app_token:
            raise BitableSchemaError("配置的 wiki 节点不是多维表格")
        if location.app_token is not None and location.app_token != app_token:
            raise BitableSchemaError("多维表格 app_token 与 wiki 节点不一致")
        return location.model_copy(update={"app_token": app_token})

    async def ensure_schema(self, location: BitableLocation) -> ProductionSchema:
        fields = await self._client.iter_items(self._fields_path(location))
        by_name: dict[str, dict] = {}
        for field in fields:
            name = field.get("field_name")
            if isinstance(name, str) and name in _REQUIRED_FIELDS:
                if name in by_name:
                    raise BitableSchemaError(f"多维表格存在重复字段：{name}")
                by_name[name] = field

        field_ids: dict[str, str] = {}
        for name, types in _REQUIRED_FIELDS.items():
            field = by_name.get(name)
            if field is None:
                raise BitableSchemaError(f"生产多维表格缺少字段：{name}")
            if field.get("type") not in types:
                raise BitableSchemaError(f"生产多维表格字段类型不兼容：{name}")
            field_id = field.get("field_id")
            if not isinstance(field_id, str) or not field_id:
                raise BitableSchemaError(f"生产多维表格字段缺少 field_id：{name}")
            field_ids[name] = field_id
        return ProductionSchema(
            requirement_name_field_id=field_ids["需求名称"],
            task_type_field_id=field_ids["需求类型"],
            requirement_attachment_field_id=field_ids["需求附件"],
            project_name_field_id=field_ids["项目名称"],
            requester_field_id=field_ids["发起人"],
            maker_field_id=field_ids["需求制作人"],
            progress_field_id=field_ids["当前进度"],
        )

    async def list_tasks(
        self,
        location: BitableLocation,
        schema: ProductionSchema,
        *,
        include_completed: bool,
    ) -> list[ProductionTaskSummary]:
        del schema, include_completed
        records = await self._client.iter_items(
            self._records_path(location), params={"view_id": location.view_id}
        )
        tasks: list[ProductionTaskSummary] = []
        for record in records:
            task = _to_task(record)
            if task is not None and task.progress != _COMPLETED_PROGRESS:
                tasks.append(task)
        return tasks

    @staticmethod
    def _fields_path(location: BitableLocation) -> str:
        return f"/open-apis/bitable/v1/apps/{_app_token(location)}/tables/{location.table_id}/fields"

    @staticmethod
    def _records_path(location: BitableLocation) -> str:
        return f"/open-apis/bitable/v1/apps/{_app_token(location)}/tables/{location.table_id}/records"


def _to_task(record: Mapping[str, Any]) -> ProductionTaskSummary | None:
    fields = record.get("fields")
    record_id = record.get("record_id")
    if not isinstance(fields, Mapping) or not isinstance(record_id, str) or not record_id:
        return None
    try:
        source_url = parse_requirement_source(fields.get("需求附件"))
    except (TypeError, ValueError):
        return None
    progress = _text(fields.get("当前进度"))
    task_type = _text(fields.get("需求类型"))
    requirement_name = _text(fields.get("需求名称"))
    if not progress or not requirement_name:
        return None
    requester_ids, requester_names = _people(fields.get("发起人"))
    maker_ids, maker_names = _people(fields.get("需求制作人"))
    snapshot = ProductionSourceSnapshot(
        requirement_name=requirement_name,
        task_type=task_type,
        requirement_attachment=source_url,
        project_names=_texts(fields.get("项目名称")),
        requester_open_ids=requester_ids,
        requester_names=requester_names,
        maker_open_ids=maker_ids,
        maker_names=maker_names,
    )
    return ProductionTaskSummary(
        record_id=record_id,
        display_text=requirement_name,
        source_url=source_url,
        progress=progress,
        task_type=task_type,
        maker_open_id=maker_ids[0] if len(maker_ids) == 1 else None,
        maker_name=maker_names[0] if len(maker_names) == 1 else None,
        snapshot=snapshot,
    )


def _app_token(location: BitableLocation) -> str:
    if not isinstance(location.app_token, str) or not location.app_token:
        raise ValueError("多维表格位置尚未解析 app_token")
    return location.app_token


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        text = value.get("text")
        return text.strip() if isinstance(text, str) else ""
    if isinstance(value, list):
        return "".join(_text(item) for item in value).strip()
    return ""


def _texts(value: Any) -> list[str]:
    values = value if isinstance(value, list) else [value]
    result: list[str] = []
    for item in values:
        text = _text(item)
        if text and text not in result:
            result.append(text)
    return result


def _people(value: Any) -> tuple[list[str], list[str]]:
    if not isinstance(value, list):
        return [], []
    ids: list[str] = []
    names: list[str] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        identity = item.get("open_id") or item.get("id") or item.get("user_id")
        if isinstance(identity, str) and identity and identity not in ids:
            ids.append(identity)
        name = item.get("name")
        if isinstance(name, str) and name.strip() and name.strip() not in names:
            names.append(name.strip())
    return ids, names
