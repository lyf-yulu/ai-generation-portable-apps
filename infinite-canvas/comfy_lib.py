"""ComfyUI 工作流库：受控解析、预览与生命周期管理（执行暂未启用）。

与上游 server/ai_creation_canvas/comfy/ 对齐：
- workflow_json.py 的解析/导出原样移植（拒绝敏感字段名、端点别名、
  超限 JSON，只输出安全的预览投影）
- library.py 的生命周期语义（乐观锁版本、enable 前必须存在文档修订）
- service.py 的健康探测（GET /object_info 取节点类型清单）

与上游一致的取舍：execution_available 恒为 False —— 执行切片尚未交付，
画布节点只做展示与连线（GraphComfyWorkflowMetadata.executionEnabled: false）。

配置：state/comfyui-services.json（gitignored），模板
config/comfyui-services.example.json。缺配置时服务列表为空，
库的导入/预览/导出照常可用（惰性库不依赖 ComfyUI 实例在线）。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import httpx

import store

STATE_DIR = Path(__file__).resolve().parent / "state"
SERVICES_PATH = STATE_DIR / "comfyui-services.json"

_MAX_BYTES = 4 * 1024 * 1024
_MAX_NODES = 500
_MAX_LINKS = 2_000
_MAX_DEPTH = 64
_MAX_STRING_BYTES = 64 * 1024
_MAX_API_NODE_ID_CHARS = 64

_SERVICE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")

_CORE_NODE_TYPES = frozenset({"LoadImage", "SaveImage", "LoadImageMask", "LoadLatent", "SaveImageWebsocket"})


class WorkflowFormat(StrEnum):
    EDITOR = "editor"
    API = "api"


class WorkflowValidationError(ValueError):
    """稳定的校验错误码，消息里不含输入数据。"""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


# ------------------------------------------------------------------- 解析

def _canonical_field_name(value: str) -> str:
    return "".join(c for c in value.casefold() if c.isalnum())


_FORBIDDEN_FIELD_NAMES = frozenset(_canonical_field_name(v) for v in {
    "apikey", "auth", "auth_header", "auth_token", "authorization",
    "access_token", "refresh_token", "credential", "credentials",
    "credential_ref", "header", "headers", "key", "password", "private_key",
    "public_key", "secret", "secret_ref", "script", "scripts", "plugin",
    "plugins", "code", "token", "webhook", "auth_header_ref", "endpoint",
    "base_url", "callback_url", "service_url", "server_url", "webhook_url",
    "endpoint_url", "service_endpoint", "callback_endpoint", "base_endpoint",
    "base_endpoint_url", "base_url_endpoint", "webhook_endpoint",
    "webhook_endpoint_url", "webhook_url_endpoint", "server_endpoint",
    "server_endpoint_url", "server_url_endpoint",
})
_CONTROL_URL_MARKERS = frozenset({"base", "callback", "service", "server", "webhook"})


def _is_forbidden_field_name(value: str) -> bool:
    name = _canonical_field_name(value)
    return (name in _FORBIDDEN_FIELD_NAMES
            or "endpoint" in name
            or ("url" in name and any(m in name for m in _CONTROL_URL_MARKERS)))


@dataclass(frozen=True, slots=True)
class PreviewNode:
    id: str
    type: str
    title: str | None
    position: tuple[int, int] | None


@dataclass(frozen=True, slots=True)
class PreviewEdge:
    source_id: str
    target_id: str


@dataclass(frozen=True, slots=True)
class PreviewGraph:
    nodes: tuple[PreviewNode, ...]
    edges: tuple[PreviewEdge, ...]
    has_editor_layout: bool


@dataclass(frozen=True, slots=True)
class ParsedWorkflow:
    raw: dict
    checksum: str
    formats: frozenset[WorkflowFormat]
    node_count: int
    link_count: int
    node_types: frozenset[str]
    preview: PreviewGraph


def canonical_checksum(value: object) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
    except UnicodeEncodeError as error:
        raise WorkflowValidationError("WORKFLOW_ENCODING_INVALID") from error
    except (TypeError, ValueError) as error:
        raise WorkflowValidationError("WORKFLOW_JSON_INVALID") from error
    return hashlib.sha256(encoded).hexdigest()


def parse_workflow_json(raw: bytes) -> ParsedWorkflow:
    value = _decode_json_object(raw, max_bytes=_MAX_BYTES)
    _assert_value_limits(value, depth=0)
    if isinstance(value.get("nodes"), list) and isinstance(value.get("links"), list):
        return _parse_editor(value)
    if value and all(key.isdecimal() for key in value):
        return _parse_api(value)
    raise WorkflowValidationError("WORKFLOW_FORMAT_UNSUPPORTED")


def export_workflow(parsed: ParsedWorkflow, format: WorkflowFormat) -> bytes:
    try:
        selected = WorkflowFormat(format)
    except ValueError as error:
        raise WorkflowValidationError("WORKFLOW_FORMAT_UNAVAILABLE") from error
    if selected not in parsed.formats:
        raise WorkflowValidationError("WORKFLOW_FORMAT_UNAVAILABLE")
    return json.dumps(parsed.raw, ensure_ascii=False, sort_keys=True, indent=2,
                      allow_nan=False).encode("utf-8")


def _decode_json_object(raw: bytes, *, max_bytes: int) -> dict:
    if not isinstance(raw, bytes) or len(raw) > max_bytes:
        raise WorkflowValidationError("WORKFLOW_SIZE_EXCEEDED")
    try:
        decoded = raw.decode("utf-8").removeprefix("\ufeff")
    except UnicodeDecodeError as error:
        raise WorkflowValidationError("WORKFLOW_ENCODING_INVALID") from error
    try:
        value = json.loads(decoded, object_pairs_hook=_reject_duplicate_pairs,
                           parse_constant=_reject_constant)
    except WorkflowValidationError:
        raise
    except (json.JSONDecodeError, RecursionError) as error:
        raise WorkflowValidationError("WORKFLOW_JSON_INVALID") from error
    if not isinstance(value, dict):
        raise WorkflowValidationError("WORKFLOW_FORMAT_UNSUPPORTED")
    return value


def _reject_duplicate_pairs(pairs: list) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise WorkflowValidationError("WORKFLOW_JSON_DUPLICATE_KEY")
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    raise WorkflowValidationError("WORKFLOW_JSON_NONFINITE")


def _assert_value_limits(value: object, *, depth: int) -> None:
    if depth > _MAX_DEPTH:
        raise WorkflowValidationError("WORKFLOW_DEPTH_EXCEEDED")
    if isinstance(value, str):
        if len(value.encode("utf-8")) > _MAX_STRING_BYTES:
            raise WorkflowValidationError("WORKFLOW_STRING_TOO_LARGE")
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise WorkflowValidationError("WORKFLOW_JSON_NONFINITE")
        return
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _assert_value_limits(key, depth=depth + 1)
            if _is_forbidden_field_name(key):
                raise WorkflowValidationError("WORKFLOW_FIELD_REJECTED")
            _assert_value_limits(item, depth=depth + 1)
        return
    if isinstance(value, list):
        for item in value:
            _assert_value_limits(item, depth=depth + 1)
        return
    raise WorkflowValidationError("WORKFLOW_JSON_INVALID")


def _parse_editor(value: dict) -> ParsedWorkflow:
    nodes = value["nodes"]
    links = value["links"]
    if len(nodes) > _MAX_NODES:
        raise WorkflowValidationError("WORKFLOW_NODE_LIMIT_EXCEEDED")
    if len(links) > _MAX_LINKS:
        raise WorkflowValidationError("WORKFLOW_LINK_LIMIT_EXCEEDED")
    preview_nodes: list[PreviewNode] = []
    node_ids: set = set()
    node_types: set[str] = set()
    for node in nodes:
        if not isinstance(node, dict):
            raise WorkflowValidationError("WORKFLOW_TOPOLOGY_INVALID")
        node_id = _editor_node_id(node.get("id"))
        if node_id in node_ids:
            raise WorkflowValidationError("WORKFLOW_TOPOLOGY_INVALID")
        node_type = _node_type(node.get("type"))
        node_ids.add(node_id)
        node_types.add(node_type)
        preview_nodes.append(PreviewNode(id=str(node_id), type=node_type,
                                         title=_safe_title(node.get("title")),
                                         position=_editor_position(node.get("pos"))))
    preview_edges: list[PreviewEdge] = []
    link_ids: set = set()
    for link in links:
        if not isinstance(link, list) or len(link) < 6:
            raise WorkflowValidationError("WORKFLOW_TOPOLOGY_INVALID")
        link_id = _editor_node_id(link[0])
        source_id = _editor_node_id(link[1])
        target_id = _editor_node_id(link[3])
        if link_id in link_ids or source_id not in node_ids or target_id not in node_ids:
            raise WorkflowValidationError("WORKFLOW_TOPOLOGY_INVALID")
        link_ids.add(link_id)
        preview_edges.append(PreviewEdge(source_id=str(source_id), target_id=str(target_id)))
    return _parsed(value, WorkflowFormat.EDITOR, node_count=len(nodes), link_count=len(links),
                   node_types=frozenset(node_types),
                   preview=PreviewGraph(tuple(preview_nodes), tuple(preview_edges), has_editor_layout=True))


def _parse_api(value: dict) -> ParsedWorkflow:
    if len(value) > _MAX_NODES:
        raise WorkflowValidationError("WORKFLOW_NODE_LIMIT_EXCEEDED")
    node_ids = {_api_node_id(node_id) for node_id in value}
    preview_nodes: list[PreviewNode] = []
    node_types: set[str] = set()
    preview_edges: list[PreviewEdge] = []
    for node_id in sorted(node_ids, key=lambda item: (int(item), item)):
        node = value[node_id]
        if not isinstance(node, dict):
            raise WorkflowValidationError("WORKFLOW_TOPOLOGY_INVALID")
        node_type = _node_type(node.get("class_type"))
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            raise WorkflowValidationError("WORKFLOW_TOPOLOGY_INVALID")
        node_types.add(node_type)
        preview_nodes.append(PreviewNode(id=node_id, type=node_type, title=None, position=None))
        for input_value in inputs.values():
            source_id = _api_link_source(input_value)
            if source_id is None:
                continue
            if source_id not in node_ids:
                raise WorkflowValidationError("WORKFLOW_TOPOLOGY_INVALID")
            preview_edges.append(PreviewEdge(source_id=source_id, target_id=node_id))
            if len(preview_edges) > _MAX_LINKS:
                raise WorkflowValidationError("WORKFLOW_LINK_LIMIT_EXCEEDED")
    return _parsed(value, WorkflowFormat.API, node_count=len(value), link_count=len(preview_edges),
                   node_types=frozenset(node_types),
                   preview=PreviewGraph(tuple(preview_nodes), tuple(preview_edges), has_editor_layout=False))


def _parsed(value: dict, format: WorkflowFormat, *, node_count: int, link_count: int,
            node_types: frozenset[str], preview: PreviewGraph) -> ParsedWorkflow:
    return ParsedWorkflow(raw=value, checksum=canonical_checksum(value),
                          formats=frozenset({format}), node_count=node_count,
                          link_count=link_count, node_types=node_types, preview=preview)


def _editor_node_id(value: object):
    if isinstance(value, bool) or not isinstance(value, (int, str)) or (isinstance(value, str) and not value):
        raise WorkflowValidationError("WORKFLOW_TOPOLOGY_INVALID")
    if isinstance(value, str):
        _assert_safe_preview_text(value)
    return value


def _node_type(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkflowValidationError("WORKFLOW_TOPOLOGY_INVALID")
    _assert_safe_preview_text(value)
    return value


def _editor_position(value: object) -> tuple[int, int] | None:
    if not isinstance(value, list) or len(value) != 2:
        return None
    x, y = value
    if isinstance(x, bool) or isinstance(y, bool) or not isinstance(x, int) or not isinstance(y, int):
        return None
    return x, y


def _safe_title(value: object) -> str | None:
    if not isinstance(value, str) or not _is_safe_preview_text(value):
        return None
    return value


def _api_node_id(value: str) -> str:
    if not value.isascii() or not value.isdecimal() or len(value) > _MAX_API_NODE_ID_CHARS:
        raise WorkflowValidationError("WORKFLOW_TOPOLOGY_INVALID")
    return value


def _is_safe_preview_text(value: str) -> bool:
    return "<" not in value and ">" not in value


def _assert_safe_preview_text(value: str) -> None:
    if not _is_safe_preview_text(value):
        raise WorkflowValidationError("WORKFLOW_FIELD_REJECTED")


def _api_link_source(value: object) -> str | None:
    if not isinstance(value, list) or len(value) != 2:
        return None
    source_id, output_index = value
    if isinstance(source_id, bool) or not isinstance(source_id, (str, int)):
        return None
    if isinstance(output_index, bool) or not isinstance(output_index, int):
        return None
    return str(source_id)


# -------------------------------------------------------------------- 库

def _revision_values(parsed: ParsedWorkflow) -> dict:
    if len(parsed.formats) != 1:
        raise WorkflowValidationError("WORKFLOW_FORMAT_UNSUPPORTED")
    format = next(iter(parsed.formats))
    encoded = export_workflow(parsed, format).decode("utf-8")
    node_inventory = {"node_count": parsed.node_count, "link_count": parsed.link_count,
                      "nodes": [{"id": n.id, "type": n.type, "title": n.title}
                                for n in parsed.preview.nodes]}
    dependency_inventory = {"node_types": [
        {"type": t, "is_core": t in _CORE_NODE_TYPES} for t in sorted(parsed.node_types)]}
    return {
        "source_filename": "workflow.json",
        "editor_json": encoded if format is WorkflowFormat.EDITOR else None,
        "api_json": encoded if format is WorkflowFormat.API else None,
        "editor_checksum": parsed.checksum if format is WorkflowFormat.EDITOR else None,
        "api_checksum": parsed.checksum if format is WorkflowFormat.API else None,
        "node_inventory_json": json.dumps(node_inventory, ensure_ascii=False, sort_keys=True,
                                          separators=(",", ":")),
        "dependency_inventory_json": json.dumps(dependency_inventory, ensure_ascii=False,
                                                sort_keys=True, separators=(",", ":")),
    }


def _require_text(value: str, field: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise ValueError(f"workflow {field} is invalid")


def create_template(display_name: str, service_id: str, parsed: ParsedWorkflow,
                    *, actor_user_id: str) -> dict:
    _require_text(display_name, "display name")
    _require_text(service_id, "service id")
    _require_text(actor_user_id, "actor user id")
    workflow_id = f"cw-{secrets.token_urlsafe(24)}"
    return store.create_comfy_workflow(workflow_id, display_name, service_id,
                                       actor_user_id, **_revision_values(parsed))


def add_revision(workflow_id: str, parsed: ParsedWorkflow, *, expected_revision: int,
                 actor_user_id: str) -> dict:
    if type(expected_revision) is not int or expected_revision < 1:
        raise WorkflowValidationError("WORKFLOW_REVISION_CONFLICT")
    _require_text(actor_user_id, "actor user id")
    try:
        return store.add_comfy_workflow_revision(workflow_id, expected_revision,
                                                 actor_user_id, **_revision_values(parsed))
    except ValueError as error:
        raise WorkflowValidationError(str(error)) from error


def set_lifecycle(workflow_id: str, *, expected_revision: int, enabled: bool | None = None,
                  archived: bool | None = None) -> dict:
    if type(expected_revision) is not int or expected_revision < 1:
        raise WorkflowValidationError("WORKFLOW_REVISION_CONFLICT")
    try:
        return store.set_comfy_workflow_lifecycle(workflow_id, expected_revision,
                                                  enabled=enabled, archived=archived)
    except (ValueError, KeyError) as error:
        raise WorkflowValidationError("WORKFLOW_REVISION_CONFLICT") from error


def export_revision(workflow_id: str, revision: int, format: WorkflowFormat) -> bytes:
    if type(revision) is not int or revision < 1:
        raise WorkflowValidationError("WORKFLOW_REVISION_CONFLICT")
    record = store.comfy_workflow_revision(workflow_id, revision)
    if record is None:
        raise KeyError((workflow_id, revision))
    try:
        selected = WorkflowFormat(format)
    except ValueError as error:
        raise WorkflowValidationError("WORKFLOW_FORMAT_UNAVAILABLE") from error
    raw = record[f"{selected.value}_json"]
    if not isinstance(raw, str):
        raise WorkflowValidationError("WORKFLOW_FORMAT_UNAVAILABLE")
    return export_workflow(parse_workflow_json(raw.encode("utf-8")), selected)


# ------------------------------------------------------------- 服务与健康

def load_services() -> list[dict]:
    """读取 state/comfyui-services.json。缺失/非法条目一律跳过。"""
    try:
        data = json.loads(SERVICES_PATH.read_bytes())
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict) or not isinstance(data.get("services"), list):
        return []
    services: list[dict] = []
    for entry in data["services"]:
        if not isinstance(entry, dict):
            continue
        service_id = entry.get("service_id")
        base_url = entry.get("base_url")
        timeout = entry.get("timeout_seconds", 10)
        if (not isinstance(service_id, str) or _SERVICE_ID.fullmatch(service_id) is None
                or not isinstance(base_url, str) or not base_url
                or type(timeout) is not int or not 1 <= timeout <= 60):
            continue
        auth_ref = entry.get("auth_header_ref")
        if auth_ref is not None and (not isinstance(auth_ref, str) or not auth_ref.strip()):
            continue
        services.append({"service_id": service_id, "base_url": base_url,
                         "timeout_seconds": timeout, "auth_header_ref": auth_ref})
    return services


def probe_service(service: dict) -> dict:
    """GET /object_info 探测健康与节点清单。失败一律归为 unavailable。"""
    result = {"service_id": service["service_id"], "status": "unavailable", "node_types": []}
    try:
        with httpx.Client(base_url=service["base_url"],
                          timeout=float(service["timeout_seconds"])) as client:
            response = client.get("/object_info")
        payload = response.json()
        node_types = list(payload) if isinstance(payload, dict) else []
        if not node_types or any(not isinstance(v, str) or not v or len(v) > 128 for v in node_types):
            return result
        result["status"] = "healthy"
        result["node_types"] = sorted(set(node_types))
    except Exception:
        pass
    return result
