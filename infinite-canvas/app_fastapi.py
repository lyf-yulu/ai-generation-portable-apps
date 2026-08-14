"""无限画布子应用 —— FastAPI 入口。

由 Portal 通过共享 .venv 的 uvicorn 启动（portal/app.py:641-657）：
    uvicorn app_fastapi:app --host 127.0.0.1 --port 8893
需要 launchd plist 里设置 INFINITE_CANVAS_ENGINE=fastapi。

边界：只监听回环；身份来自 Portal 的 HMAC 签名头；不做 CSRF
（X-CSRF-Token 不在 Portal 转发白名单内，做了必然全挂）。
"""

from __future__ import annotations

import mimetypes
import re
import secrets
from pathlib import Path

from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse, Response

import store
import translate
from portal_identity import verify_portal_identity

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

# 上限沿用画布上游默认值；Portal 自身上限 200MB（portal/app.py:1293），我们更严格。
MAX_UPLOAD = {"image": 10 * 1024 * 1024, "video": 64 * 1024 * 1024, "audio": 32 * 1024 * 1024}
_EXT_BY_MIME = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif",
    "video/mp4": ".mp4", "video/webm": ".webm", "video/quicktime": ".mov",
    "audio/mpeg": ".mp3", "audio/wav": ".wav", "audio/x-wav": ".wav",
}
_ASSET_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

app = FastAPI(title="infinite-canvas", docs_url=None, redoc_url=None, openapi_url=None)


def _error(status: int, code: str, message: str, *, retryable: bool = False,
           phase: str = "request") -> JSONResponse:
    """错误体形状对齐前端 ApiError（web/src/api/contracts.ts:19）。"""
    return JSONResponse(
        status_code=status,
        content={"code": code, "message": message, "retryable": retryable,
                 "request_id": "canvas", "phase": phase},
    )


@app.on_event("startup")
def _startup() -> None:
    # 建表很快；不要在启动路径做重活 —— Portal 看门狗只给约 45 秒
    # （portal/app.py:685-715 每 15s 探活、连续 3 次失败即重启）。
    store.init_schema()


def _is_loopback(request: Request) -> bool:
    host = (request.client.host if request.client else "") or ""
    return host in ("127.0.0.1", "::1", "localhost")


@app.middleware("http")
async def identity_boundary(request: Request, call_next):
    path = request.url.path
    # Portal 的统计轮询是服务端到服务端直连子应用（portal/app.py:986-987 用裸
    # HTTPConnection），**不经代理、不注入任何签名头**。这个兼容路由专供它使用，
    # 因此只能以「来源必须是回环」为边界，不能要求签名。
    # 它返回的字段仅任务状态与计数，不含用户名或素材内容。
    # 画布前端用的是 /api/v1/jobs/{id}，仍然要求签名。
    if path.startswith("/api/jobs/"):
        if not _is_loopback(request):
            return _error(404, "not_found", "资源不存在。")
        return await call_next(request)
    # 静态资源不校验身份：Portal 代理层已要求登录（portal/app.py:1980 检查 use_apps），
    # 这里重复校验只会让 SPA 白屏难排查。
    if path.startswith("/api/"):
        user = verify_portal_identity(request.headers)
        if user is None:
            return _error(401, "unauthorized", "请通过 Portal 访问。", phase="authentication")
        request.state.user = user
    return await call_next(request)


# ------------------------------------------------------------------ session

@app.get("/api/v1/session")
async def get_session(request: Request):
    user = request.state.user
    # user_id 决定前端 IndexedDB 库名 ai-creation-canvas:<env>:<userId>
    # （web/src/storage/scope.ts:25-27），必须稳定 —— 不能用 username 代替。
    return {"user_id": user["user_id"], "username": user["username"], "role": user["role"]}


# ----------------------------------------------------------------- projects

@app.get("/api/v1/projects")
async def api_list_projects(request: Request):
    return {"projects": store.list_projects(request.state.user["user_id"])}


@app.post("/api/v1/projects")
async def api_create_project(request: Request):
    user = request.state.user
    document = await request.json()
    if not isinstance(document, dict) or not _PROJECT_ID_RE.match(str(document.get("id") or "")):
        return _error(400, "invalid_request", "画布标识无效。")
    try:
        envelope, created = store.create_project(user["user_id"], document)
    except store.DocumentTooLarge as exc:
        return _error(413, "document_too_large", str(exc))
    except store.ConflictError:
        return _error(409, "PROJECT_CONFLICT", "画布已存在且内容不同。")
    return JSONResponse(status_code=201 if created else 200, content=envelope)


@app.get("/api/v1/projects/{project_id}")
async def api_get_project(request: Request, project_id: str):
    try:
        return store.get_project(request.state.user["user_id"], project_id)
    except store.NotFoundError:
        return _error(404, "not_found", "画布不存在。")


@app.put("/api/v1/projects/{project_id}")
async def api_update_project(request: Request, project_id: str):
    payload = await request.json()
    if not isinstance(payload, dict):
        return _error(400, "invalid_request", "请求体无效。")
    expected = payload.pop("expected_version", None)
    if not isinstance(expected, int):
        return _error(400, "invalid_request", "缺少版本号。")
    try:
        return store.update_project(request.state.user["user_id"], project_id, payload, expected)
    except store.DocumentTooLarge as exc:
        return _error(413, "document_too_large", str(exc))
    except store.ConflictError:
        # code 必须精确等于 PROJECT_CONFLICT —— 前端靠它触发“冲突副本”分叉
        # （web/src/features/projects/project-sync.ts:62-65,297），拼错会丢改动。
        return _error(409, "PROJECT_CONFLICT", "画布已被其他窗口修改。")


@app.delete("/api/v1/projects/{project_id}", status_code=204)
async def api_delete_project(request: Request, project_id: str):
    store.delete_project(request.state.user["user_id"], project_id)
    return Response(status_code=204)


# ------------------------------------------------------------------- assets

@app.post("/api/v1/assets")
async def api_upload_asset(request: Request, file: UploadFile = File(...),
                           media_type: str = Form(...), kind: str = Form("reference")):
    user = request.state.user
    if media_type not in ("image", "video", "audio"):
        return _error(400, "invalid_request", "媒体类型无效。")
    payload = await file.read()
    if not payload:
        # 前端要求 size_bytes >= 1（web/src/api/assets.ts:24），空文件必须拒绝。
        return _error(400, "invalid_request", "文件为空。")
    if len(payload) > MAX_UPLOAD[media_type]:
        return _error(413, "file_too_large", "文件超过大小上限。")

    # mime 自行推导：前端严格校验 mime_type 必须以 "<media_type>/" 开头
    # （web/src/api/assets.ts:23），不能直接信客户端的 Content-Type。
    guessed = mimetypes.guess_type(file.filename or "")[0] or ""
    mime = guessed if guessed.startswith(f"{media_type}/") else f"{media_type}/octet-stream"
    if not guessed.startswith(f"{media_type}/"):
        declared = (file.content_type or "")
        mime = declared if declared.startswith(f"{media_type}/") else mime

    asset_id = secrets.token_urlsafe(16).replace("=", "")
    # 绝不用客户端文件名做路径（目录穿越）；扩展名由 mime 白名单反查。
    ext = _EXT_BY_MIME.get(mime, ".bin")
    user_dir = store.UPLOAD_DIR / re.sub(r"[^A-Za-z0-9_-]", "_", user["user_id"])[:64]
    user_dir.mkdir(parents=True, exist_ok=True)
    path = user_dir / f"{asset_id}{ext}"
    path.write_bytes(payload)

    return store.insert_asset(user["user_id"], asset_id, media_type, mime, len(payload), str(path))


@app.get("/api/v1/assets/{asset_id}")
async def api_get_asset(request: Request, asset_id: str):
    if not _ASSET_ID_RE.match(asset_id):
        return _error(400, "invalid_request", "资产标识无效。")
    try:
        row = store.get_asset(request.state.user["user_id"], asset_id)
    except store.NotFoundError:
        return _error(404, "not_found", "资产不存在。")
    return {"asset_id": row["asset_id"], "kind": "reference", "status": "active",
            "media_type": row["media_type"], "mime_type": row["mime_type"],
            "size_bytes": int(row["size_bytes"])}


@app.get("/api/v1/assets/{asset_id}/content")
@app.head("/api/v1/assets/{asset_id}/content")
async def api_asset_content(request: Request, asset_id: str):
    if not _ASSET_ID_RE.match(asset_id):
        return _error(400, "invalid_request", "资产标识无效。")
    try:
        row = store.get_asset(request.state.user["user_id"], asset_id)
    except store.NotFoundError:
        return _error(404, "not_found", "资产不存在。")
    path = Path(row["path"])
    if not path.is_file():
        return _error(404, "not_found", "资产文件缺失。")
    # FileResponse 自动处理 Range —— <video> 需要 Range 拿 metadata 才能画首帧。
    return FileResponse(str(path), media_type=row["mime_type"])


@app.delete("/api/v1/assets/{asset_id}", status_code=204)
async def api_delete_asset(request: Request, asset_id: str):
    try:
        store.delete_asset(request.state.user["user_id"], asset_id)
    except store.NotFoundError:
        return _error(404, "not_found", "资产不存在。")
    return Response(status_code=204)


# ----------------------------------------------------------------- activity

@app.get("/api/v1/activity/assets")
async def api_activity_assets(request: Request):
    rows = store.list_assets(request.state.user["user_id"])
    return {"assets": [
        {"asset_id": r["asset_id"], "media_type": r["media_type"], "mime_type": r["mime_type"],
         "size_bytes": int(r["size_bytes"]), "created_at": r["created_at"]}
        for r in rows
    ]}


@app.get("/api/v1/activity/jobs")
async def api_activity_jobs(request: Request):
    jobs = store.list_jobs(request.state.user["user_id"])
    return {"jobs": [
        {"job_id": j["job_id"], "operation": j["operation"], "status": j["status"],
         "created_at": j["created_at"]}
        for j in jobs
    ]}


@app.get("/api/v1/prompt-skills")
async def api_prompt_skills():
    # 提示词优化 Skill 需要额外的文本模型接入，当前不提供。
    return {"skills": []}


# --------------------------------------------------------- models / jobs

app.include_router(translate.router)


# ------------------------------------------------------------ static / SPA

@app.get("/{requested_path:path}")
async def static_or_spa(requested_path: str):
    """静态资源，找不到则回退到 index.html（react-router 是 browser history 模式）。"""
    index = STATIC_DIR / "index.html"
    if requested_path:
        # 拒绝穿越：解析后必须仍在 STATIC_DIR 内，且是常规文件（挡符号链接）。
        candidate = (STATIC_DIR / requested_path).resolve()
        try:
            candidate.relative_to(STATIC_DIR.resolve())
        except ValueError:
            return _error(404, "not_found", "资源不存在。")
        if candidate.is_file() and not candidate.is_symlink():
            return FileResponse(str(candidate))
    if not index.is_file():
        return _error(404, "not_found", "前端尚未构建，请运行 build.sh。")
    return FileResponse(str(index), media_type="text/html")
