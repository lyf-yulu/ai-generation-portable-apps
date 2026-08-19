"""ComfyUI 工作流库 API —— 与上游 api/comfy_workflows.py 对齐的瘦移植。

管理端（仅 admin，Portal 的 X-Is-Admin 决定）：
  import / list / capabilities / get / preview / export / add-revision /
  enable / disable / archive / restore
用户端：
  GET /api/v1/comfy-workflows —— 当前放行策略为团队全员可见
  （上游 Portal 模式没有用户目录、按人授权不可用；见 store.assigned_comfy_workflows）
  PUT /admin/users/{id}/comfy-workflows —— 恒 409 WORKFLOW_ASSIGNMENT_UNAVAILABLE

execution_available 恒为 False：执行切片尚未交付，与上游一致。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi import APIRouter, Request, UploadFile, File, Form, Query
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, StrictInt

import comfy_lib
import store

router = APIRouter(prefix="/api/v1")

CONFIG_DIR = Path(__file__).resolve().parent / "config"
EXAMPLE_FILES = {
    "comfy-workflow": "comfy-workflow.example.json",
}
_WORKFLOW_JSON_MAX_BYTES = 4 * 1024 * 1024
_REVISION_TEXT = re.compile(r"[1-9][0-9]{0,9}\Z")
_CHECKSUM_PREFIX_LENGTH = 12


def _err(status: int, code: str, message: str, *, retryable: bool = False) -> JSONResponse:
    return JSONResponse(status_code=status, content={
        "code": code, "message": message, "retryable": retryable,
        "request_id": "canvas", "phase": "request"})


def _require_admin(request: Request) -> str:
    # 与上游一致：非管理员一律 404，不暴露资源存在性。
    if request.state.user.get("role") != "admin":
        raise _HTTP(404, "API_NOT_FOUND", "资源不存在。")
    return request.state.user["user_id"]


class _HTTP(Exception):
    def __init__(self, status: int, code: str, message: str, retryable: bool = False):
        self.status = status
        self.code = code
        self.message = message
        self.retryable = retryable


def _workflow_error(error: Exception) -> None:
    if isinstance(error, comfy_lib.WorkflowValidationError):
        raise _HTTP(400, error.code, "工作流请求被拒绝。") from None
    raise _HTTP(400, "REQUEST_REJECTED", "工作流请求被拒绝。") from None


def _admin_template(workflow_id: str) -> dict:
    try:
        return store.get_comfy_workflow(workflow_id)
    except KeyError:
        raise _HTTP(404, "WORKFLOW_NOT_FOUND", "工作流不存在。") from None


def _assigned_template(workflow_id: str) -> dict:
    for item in store.assigned_comfy_workflows():
        if item["workflow_id"] == workflow_id:
            return item
    raise _HTTP(404, "WORKFLOW_NOT_FOUND", "工作流不存在。")


def _latest_document_revision(item: dict) -> int:
    for revision in range(int(item["revision"]), 0, -1):
        if store.comfy_workflow_revision(item["workflow_id"], revision) is not None:
            return revision
    raise _HTTP(404, "WORKFLOW_UNAVAILABLE", "工作流文档不可用。")


def _checksum_prefix(record: dict) -> str:
    for name in ("editor_checksum", "api_checksum"):
        value = record.get(name)
        if isinstance(value, str) and len(value) >= _CHECKSUM_PREFIX_LENGTH:
            return value[:_CHECKSUM_PREFIX_LENGTH]
    raise ValueError("workflow checksum is unavailable")


def _template_projection(item: dict, *, include_checksum_prefix: bool = False) -> dict:
    revision = _latest_document_revision(item)
    value = {
        "workflow_id": item["workflow_id"],
        "display_name": item["display_name"],
        "description": item["description"],
        "service_id": item["service_id"],
        "lifecycle": {"enabled": bool(item["enabled"]), "archived": item["archived_at"] is not None},
        "revision": revision,
        "lifecycle_revision": int(item["revision"]),
        "execution_available": False,
    }
    if include_checksum_prefix:
        record = store.comfy_workflow_revision(item["workflow_id"], revision)
        if record is None:
            raise _HTTP(404, "WORKFLOW_UNAVAILABLE", "工作流文档不可用。")
        try:
            value["checksum_prefix"] = _checksum_prefix(record)
        except ValueError:
            raise _HTTP(404, "WORKFLOW_UNAVAILABLE", "工作流文档不可用。") from None
    return value


def _revision_projection(workflow_id: str, revision: int,
                         *, include_checksum_prefix: bool = False) -> dict:
    record = store.comfy_workflow_revision(workflow_id, revision)
    if record is None:
        raise _HTTP(404, "WORKFLOW_NOT_FOUND", "工作流版本不存在。")
    formats = [name for name in ("editor", "api") if record[f"{name}_json"] is not None]
    try:
        selected = comfy_lib.WorkflowFormat(formats[0])
        parsed = comfy_lib.parse_workflow_json(
            comfy_lib.export_revision(workflow_id, revision, selected))
        dependencies = json.loads(str(record["dependency_inventory_json"]))
    except (comfy_lib.WorkflowValidationError, ValueError, TypeError, KeyError, IndexError):
        raise _HTTP(404, "WORKFLOW_UNAVAILABLE", "工作流文档不可用。") from None
    value = {
        "workflow_id": workflow_id,
        "revision": revision,
        "formats": formats,
        "preview": {
            "nodes": [{"id": n.id, "type": n.type, "title": n.title,
                       "position": list(n.position) if n.position else None}
                      for n in parsed.preview.nodes],
            "edges": [{"source_id": e.source_id, "target_id": e.target_id}
                      for e in parsed.preview.edges],
            "has_editor_layout": parsed.preview.has_editor_layout,
        },
        "dependencies": dependencies,
        "execution_available": False,
        "execution_unavailable_reason": "EXECUTION_NOT_IMPLEMENTED",
    }
    if include_checksum_prefix:
        try:
            value["checksum_prefix"] = _checksum_prefix(record)
        except ValueError:
            raise _HTTP(404, "WORKFLOW_UNAVAILABLE", "工作流文档不可用。") from None
    return value


def _export(workflow_id: str, revision: int, format: str) -> Response:
    try:
        selected = comfy_lib.WorkflowFormat(format)
    except ValueError:
        raise _HTTP(400, "WORKFLOW_FORMAT_UNAVAILABLE", "该格式不可用。") from None
    try:
        content = comfy_lib.export_revision(workflow_id, revision, selected)
    except KeyError:
        raise _HTTP(404, "WORKFLOW_NOT_FOUND", "工作流版本不存在。") from None
    except comfy_lib.WorkflowValidationError as error:
        raise _HTTP(400, error.code, "该格式不可用。") from None
    filename = f"comfy-workflow-{workflow_id}-r{revision}-{selected.value}.json"
    return Response(content=content, media_type="application/json; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def _bounded_upload_bytes(upload: UploadFile) -> bytes:
    data = upload.file.read(_WORKFLOW_JSON_MAX_BYTES + 1)
    if len(data) > _WORKFLOW_JSON_MAX_BYTES:
        raise comfy_lib.WorkflowValidationError("WORKFLOW_SIZE_EXCEEDED")
    return data


# ---------------------------------------------------------------- admin API

@router.post("/admin/comfy-workflows/import", status_code=201)
def import_workflow(request: Request, file: UploadFile = File(...),
                    display_name: str = Form(...), service_id: str = Form(...)):
    actor = _require_admin(request)
    if not isinstance(file.content_type, str) or file.content_type != "application/json":
        return _err(400, "REQUEST_REJECTED", "工作流请求被拒绝。")
    try:
        parsed = comfy_lib.parse_workflow_json(_bounded_upload_bytes(file))
        item = comfy_lib.create_template(display_name, service_id, parsed,
                                         actor_user_id=actor)
    except (ValueError, comfy_lib.WorkflowValidationError) as error:
        _workflow_error(error)
    return _template_projection(item, include_checksum_prefix=True)


@router.get("/admin/comfy-workflows")
def admin_list_workflows(request: Request):
    _require_admin(request)
    return {"workflows": [_template_projection(item, include_checksum_prefix=True)
                          for item in store.list_comfy_workflows()]}


@router.get("/admin/comfy-workflows/capabilities")
def admin_workflow_capabilities(request: Request):
    _require_admin(request)
    services = [comfy_lib.probe_service(service) for service in comfy_lib.load_services()]
    return {
        # Portal 没有用户目录端口，按人授权不可用 —— 与上游 Portal 模式一致。
        "assignments": {"available": False, "reason": "PORTAL_USER_DIRECTORY_UNAVAILABLE"},
        "services": services,
    }


@router.get("/admin/comfy-workflows/{workflow_id}")
def admin_get_workflow(workflow_id: str, request: Request):
    _require_admin(request)
    item = _admin_template(workflow_id)
    return {
        **_template_projection(item, include_checksum_prefix=True),
        "current_revision": _revision_projection(
            workflow_id, _latest_document_revision(item), include_checksum_prefix=True),
    }


@router.get("/admin/comfy-workflows/{workflow_id}/revisions/{revision}/preview")
def admin_preview_workflow(workflow_id: str, revision: int, request: Request):
    _require_admin(request)
    _admin_template(workflow_id)
    return _revision_projection(workflow_id, revision, include_checksum_prefix=True)


@router.get("/admin/comfy-workflows/{workflow_id}/revisions/{revision}/export")
def admin_export_workflow(workflow_id: str, revision: int, request: Request,
                          format: str = Query(...)):
    _require_admin(request)
    _admin_template(workflow_id)
    return _export(workflow_id, revision, format)


@router.post("/admin/comfy-workflows/{workflow_id}/revisions", status_code=201)
def add_workflow_revision(workflow_id: str, request: Request,
                          file: UploadFile = File(...), revision: str = Form(...)):
    actor = _require_admin(request)
    current = _admin_template(workflow_id)
    if current["enabled"]:
        raise _HTTP(409, "WORKFLOW_REVISION_CONFLICT", "工作流必须先停用才能更新版本。")
    if not isinstance(revision, str) or _REVISION_TEXT.fullmatch(revision) is None:
        return _err(400, "REQUEST_REJECTED", "工作流请求被拒绝。")
    expected = int(revision)
    try:
        parsed = comfy_lib.parse_workflow_json(_bounded_upload_bytes(file))
        item = comfy_lib.add_revision(workflow_id, parsed, expected_revision=expected,
                                      actor_user_id=actor)
    except (ValueError, comfy_lib.WorkflowValidationError) as error:
        _workflow_error(error)
    return _template_projection(item, include_checksum_prefix=True)


class LifecycleRevision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    revision: StrictInt


def _transition(workflow_id: str, request: Request, payload: LifecycleRevision,
                *, enabled: bool | None = None, archived: bool | None = None):
    _require_admin(request)
    if payload.revision < 1:
        return _err(400, "REQUEST_REJECTED", "工作流请求被拒绝。")
    try:
        item = comfy_lib.set_lifecycle(workflow_id, expected_revision=payload.revision,
                                       enabled=enabled, archived=archived)
    except comfy_lib.WorkflowValidationError as error:
        _workflow_error(error)
    return _template_projection(item, include_checksum_prefix=True)


@router.post("/admin/comfy-workflows/{workflow_id}/enable")
def enable_workflow(workflow_id: str, request: Request, payload: LifecycleRevision):
    _require_admin(request)
    current = _admin_template(workflow_id)
    # enable 前必须有文档修订，且 service 必须在配置里 —— 与上游一致。
    _latest_document_revision(current)
    configured = {s["service_id"] for s in comfy_lib.load_services()}
    if current["service_id"] not in configured:
        raise _HTTP(409, "WORKFLOW_SERVICE_UNAVAILABLE", "工作流服务不可用。")
    return _transition(workflow_id, request, payload, enabled=True)


@router.post("/admin/comfy-workflows/{workflow_id}/disable")
def disable_workflow(workflow_id: str, request: Request, payload: LifecycleRevision):
    _require_admin(request)
    return _transition(workflow_id, request, payload, enabled=False)


@router.post("/admin/comfy-workflows/{workflow_id}/archive")
def archive_workflow(workflow_id: str, request: Request, payload: LifecycleRevision):
    _require_admin(request)
    return _transition(workflow_id, request, payload, archived=True)


@router.post("/admin/comfy-workflows/{workflow_id}/restore")
def restore_workflow(workflow_id: str, request: Request, payload: LifecycleRevision):
    _require_admin(request)
    return _transition(workflow_id, request, payload, archived=False)


@router.put("/admin/users/{user_id}/comfy-workflows")
def replace_workflow_assignments(user_id: str, request: Request):
    _require_admin(request)
    # 与上游 Portal 模式一致：没有用户目录，按人授权不可用。
    return _err(409, "WORKFLOW_ASSIGNMENT_UNAVAILABLE", "该部署形态不支持按人授权工作流。")


# ----------------------------------------------------------------- user API

@router.get("/comfy-workflows")
def list_assigned_workflows(request: Request):
    return {"workflows": [_template_projection(item)
                          for item in store.assigned_comfy_workflows()]}


@router.get("/comfy-workflows/{workflow_id}")
def get_assigned_workflow(workflow_id: str, request: Request):
    item = _assigned_template(workflow_id)
    return {**_template_projection(item),
            "current_revision": _revision_projection(
                workflow_id, _latest_document_revision(item))}


# ------------------------------------------------------------ config 示例

@router.get("/admin/config-examples/{kind}")
def download_config_example(kind: str, request: Request):
    _require_admin(request)
    filename = EXAMPLE_FILES.get(kind)
    if filename is None:
        return _err(404, "not_found", "示例不存在。")
    path = CONFIG_DIR / filename
    if not path.is_file():
        return _err(404, "not_found", "示例不存在。")
    return Response(content=path.read_bytes(), media_type="application/json; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})
