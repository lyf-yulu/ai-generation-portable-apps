#!/usr/bin/env python3
"""FastAPI port of volcengine-portrait's HTTP layer.

volcengine has 40+ endpoints across virtual + real portrait workflows,
plus SigV4-signed calls to Ark's OpenAPI and TOS upload. Instead of porting
every handler by hand (~2000 lines), we use a _LegacyBridge shim: turn each
FastAPI Request into a fake handler with rfile / wfile / send_response, call
the existing handle_*(handler) function, then translate what the shim captured
back into a FastAPI Response. This preserves all 25 handle_* functions
unchanged, including the SigV4 signing paths, while getting FastAPI's free
ETag / Range / auto multipart / nosniff middleware.

Run:
    DYLD_LIBRARY_PATH=/opt/homebrew/opt/expat/lib \
    DATA_DIR=$(pwd)/test-data PORT=8892 \
    ../.venv/bin/uvicorn app_fastapi:app --host 127.0.0.1 --port 8892
"""
from __future__ import annotations

import io
import mimetypes
import os
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
import app as legacy  # volcengine-portrait/app.py

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response as FastResponse


MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(200 * 1024 * 1024)))
STATIC_DIR = legacy.STATIC_DIR


app = FastAPI(
    title="volcengine-portrait",
    version="2.0.0-fastapi",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


# ------------------------- middleware -------------------------------


@app.middleware("http")
async def _security_and_size(request: Request, call_next):
    raw_cl = request.headers.get("content-length")
    if raw_cl:
        try:
            n = int(raw_cl)
        except (TypeError, ValueError):
            n = -1
        if n > MAX_UPLOAD_BYTES:
            return JSONResponse(
                status_code=413,
                content={"ok": False,
                         "error": f"upload too large: {n} bytes (limit {MAX_UPLOAD_BYTES})"},
            )
    resp: Response = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


_ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "").strip()
if _ALLOWED_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in _ALLOWED_ORIGINS.split(",") if o.strip()],
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS", "DELETE"],
        allow_headers=["Content-Type", "X-Workspace-Id", "X-Api-Key"],
    )


# ------------------------- Legacy handler bridge --------------------


class _LegacyBridge:
    """Fake SimpleHTTPRequestHandler that captures a handle_*() call's response.

    Behaviour needed by the volcengine handle_* functions:
    - handler.headers          → Starlette Headers (mapping-like)
    - handler.command          → 'GET' / 'POST' etc.
    - handler.path             → path + query (matches HTTP handler.path)
    - handler.rfile.read(n)    → BytesIO over request body
    - handler.wfile.write(b)   → BytesIO capture, later returned to client
    - handler.send_response(s)
    - handler.send_header(k, v)
    - handler.end_headers()
    - handler.client_address   → 2-tuple

    Once handle_*() returns, .to_response() gives a FastAPI Response with the
    captured status / headers / body."""

    def __init__(self, request: Request, body: bytes = b""):
        self.headers = request.headers
        self.command = request.method
        qs = str(request.url.query or "")
        p = request.url.path
        self.path = f"{p}?{qs}" if qs else p
        self._raw_path = self.path
        client = request.client
        self.client_address = (client.host if client else "127.0.0.1", 0)
        self.rfile = io.BytesIO(body)

        self._status = 200
        self._headers: list[tuple[str, str]] = []
        self._body = io.BytesIO()
        self.wfile = self._body

    def send_response(self, status: int, message: str | None = None):
        self._status = status

    def send_header(self, key: str, value: str):
        self._headers.append((key, value))

    def send_response_only(self, status: int, message: str | None = None):
        self._status = status

    def end_headers(self):
        pass

    def send_error(self, code: int, message: str | None = None, explain: str | None = None):
        self._status = code
        self._headers = [("Content-Type", "text/plain; charset=utf-8")]
        self._body = io.BytesIO((message or "").encode("utf-8"))
        self.wfile = self._body

    def to_response(self) -> Response:
        headers = {}
        for k, v in self._headers:
            # Skip Content-Length (Starlette recomputes) and Server
            if k.lower() in ("content-length", "server", "date", "connection"):
                continue
            headers[k] = v
        content = self._body.getvalue()
        media_type = headers.pop("Content-Type", None) or headers.pop("content-type", None)
        return FastResponse(content=content, status_code=self._status,
                            headers=headers, media_type=media_type)


async def _bridge_call(request: Request, fn: Callable, *args, **kwargs) -> Response:
    """Read the request body, spin up a _LegacyBridge, call fn(bridge, ...),
    and return the captured response."""
    body = await request.body()
    bridge = _LegacyBridge(request, body)
    try:
        fn(bridge, *args, **kwargs)
    except Exception as exc:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)})
    return bridge.to_response()


# ------------------------- top-level helpers ------------------------


def _is_local_req(request: Request) -> bool:
    ip = (request.headers.get("X-Forwarded-For") or (request.client.host if request.client else "") or "").strip()
    return ip in ("127.0.0.1", "::1", "localhost")


# ------------------------- read-only endpoints ----------------------


@app.get("/api/v1/meta")
def api_meta():
    return {
        "app": "volcengine-portrait",
        "version": "2.0.0-fastapi",
        "port": int(os.environ.get("PORT", "8891")),
        "capabilities": ["portrait-virtual", "portrait-real"],
        "status": "ready",
    }


@app.get("/api/config")
def api_config():
    return {
        "ok": True,
        "base_url": legacy.ARK_BASE_URL,
        "has_key": bool(legacy.API_KEY),
        "has_aksk": bool(legacy.ACCESS_KEY and legacy.SECRET_KEY),
        "has_api_key": bool(legacy.API_KEY),
        "has_access_key": bool(legacy.ACCESS_KEY),
        "has_secret_key": bool(legacy.SECRET_KEY),
        "output_dir": str(legacy.OUTPUT_DIR),
    }


@app.post("/api/config")
async def api_config_post(request: Request):
    return await _bridge_call(request, legacy.handle_config_post)


@app.post("/api/choose-output-dir")
def api_choose_output_dir(request: Request):
    if not _is_local_req(request):
        return {"remote": True}
    try:
        return {"path": legacy.choose_output_dir()}
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


# ------------------------- virtual portrait ------------------------


@app.get("/api/virtual/groups")
async def virtual_groups_get(request: Request):
    return await _bridge_call(request, legacy.handle_virtual_groups_get)


@app.post("/api/virtual/groups")
async def virtual_groups_post(request: Request):
    return await _bridge_call(request, legacy.handle_virtual_groups_post)


@app.post("/api/virtual/groups/purge")
async def virtual_groups_purge(request: Request):
    return await _bridge_call(request, legacy.handle_virtual_groups_purge)


@app.get("/api/virtual/groups/{group_id}")
async def virtual_group_get(request: Request, group_id: str):
    return await _bridge_call(request, legacy.handle_virtual_group_get, group_id)


@app.post("/api/virtual/groups/{group_id}")
async def virtual_group_update(request: Request, group_id: str):
    return await _bridge_call(request, legacy.handle_virtual_group_update, group_id)


@app.delete("/api/virtual/groups/{group_id}")
async def virtual_group_delete(request: Request, group_id: str):
    return await _bridge_call(request, legacy.handle_virtual_group_delete, group_id)


@app.get("/api/virtual/assets")
async def virtual_assets_get(request: Request):
    return await _bridge_call(request, legacy.handle_virtual_assets_get)


@app.post("/api/virtual/assets")
async def virtual_assets_post(request: Request):
    return await _bridge_call(request, legacy.handle_virtual_assets_post)


@app.get("/api/virtual/assets/{asset_id}")
async def virtual_asset_get(request: Request, asset_id: str):
    return await _bridge_call(request, legacy.handle_virtual_assets_get, asset_id)


@app.post("/api/virtual/assets/{asset_id}")
async def virtual_asset_update(request: Request, asset_id: str):
    return await _bridge_call(request, legacy.handle_virtual_asset_update, asset_id)


@app.delete("/api/virtual/assets/{asset_id}")
async def virtual_asset_delete(request: Request, asset_id: str):
    return await _bridge_call(request, legacy.handle_virtual_assets_delete, asset_id)


@app.get("/api/virtual/jobs")
async def virtual_jobs_get(request: Request):
    return await _bridge_call(request, legacy.handle_virtual_jobs_get)


@app.post("/api/virtual/jobs")
async def virtual_jobs_post(request: Request):
    return await _bridge_call(request, legacy.handle_virtual_jobs_post)


@app.get("/api/virtual/jobs/{job_id}")
async def virtual_job_get(request: Request, job_id: str):
    return await _bridge_call(request, legacy.handle_virtual_jobs_get, job_id)


# ------------------------- real portrait (delegates to virtual) -----


@app.get("/api/real/assets")
async def real_assets_get(request: Request):
    return await _bridge_call(request, legacy.handle_real_assets_get)


@app.post("/api/real/assets")
async def real_assets_post(request: Request):
    return await _bridge_call(request, legacy.handle_real_assets_post)


@app.get("/api/real/assets/{asset_id}")
async def real_asset_get(request: Request, asset_id: str):
    return await _bridge_call(request, legacy.handle_real_assets_get, asset_id)


@app.post("/api/real/assets/{asset_id}")
async def real_asset_update(request: Request, asset_id: str):
    return await _bridge_call(request, legacy.handle_real_asset_update, asset_id)


@app.delete("/api/real/assets/{asset_id}")
async def real_asset_delete(request: Request, asset_id: str):
    return await _bridge_call(request, legacy.handle_real_assets_delete, asset_id)


@app.get("/api/real/jobs")
async def real_jobs_get(request: Request):
    return await _bridge_call(request, legacy.handle_real_jobs_get)


@app.post("/api/real/jobs")
async def real_jobs_post(request: Request):
    return await _bridge_call(request, legacy.handle_real_jobs_post)


@app.get("/api/real/jobs/{job_id}")
async def real_job_get(request: Request, job_id: str):
    return await _bridge_call(request, legacy.handle_real_jobs_get, job_id)


@app.get("/api/real/groups")
async def real_groups_get(request: Request):
    return await _bridge_call(request, legacy.handle_real_groups_get)


@app.get("/api/real/groups/{group_id}")
async def real_group_get(request: Request, group_id: str):
    return await _bridge_call(request, legacy.handle_real_group_get, group_id)


@app.post("/api/real/groups/{group_id}")
async def real_group_update(request: Request, group_id: str):
    return await _bridge_call(request, legacy.handle_real_group_update, group_id)


@app.delete("/api/real/groups/{group_id}")
async def real_group_delete(request: Request, group_id: str):
    return await _bridge_call(request, legacy.handle_real_group_delete, group_id)


# ------------------------- jobs / activity / download ---------------


@app.get("/api/jobs")
async def api_jobs(request: Request):
    # Delegates to virtual jobs (unified job list in legacy).
    return await _bridge_call(request, legacy.handle_virtual_jobs_get)


@app.get("/api/jobs/{job_id}")
async def api_job(request: Request, job_id: str):
    return await _bridge_call(request, legacy.handle_virtual_jobs_get, job_id)


@app.get("/api/activity")
async def api_activity(request: Request):
    bridge_shim = _LegacyBridge(request)
    sees_all, username = legacy._view_scope(bridge_shim)
    return legacy.activity_list(sees_all=sees_all, username=username)


@app.get("/api/activity/{activity_id}")
async def api_activity_detail(activity_id: str, request: Request):
    record = next(
        (item for item in legacy.read_activity_log() if item.get("id") == activity_id),
        None,
    )
    body = legacy.activity_record_for_client(record) or {"error": "activity not found"}
    return JSONResponse(status_code=200 if record else 404, content=body)


@app.get("/api/download/{token}")
def api_download(token: str):
    with legacy.FILES_LOCK:
        fpath = legacy.FILES.get(token)
    if not fpath or not fpath.exists():
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(
        str(fpath),
        filename=fpath.name,
        media_type=mimetypes.guess_type(fpath.name)[0] or "application/octet-stream",
    )


# ------------------------- static + uploads/outputs ----------------


@app.get("/uploads/{path:path}")
def serve_upload(path: str):
    try:
        base = legacy.UPLOAD_DIR.resolve()
        target = (legacy.UPLOAD_DIR / urllib.parse.unquote(path)).resolve()
    except (OSError, ValueError):
        raise HTTPException(status_code=404, detail="not found")
    if not (target == base or target.is_relative_to(base)):
        raise HTTPException(status_code=403, detail="forbidden")
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(str(target),
                        media_type=mimetypes.guess_type(target.name)[0] or "application/octet-stream")


@app.get("/outputs/{path:path}")
def serve_output(path: str):
    try:
        base = legacy.OUTPUT_DIR.resolve()
        target = (legacy.OUTPUT_DIR / urllib.parse.unquote(path)).resolve()
    except (OSError, ValueError):
        raise HTTPException(status_code=404, detail="not found")
    if not (target == base or target.is_relative_to(base)):
        raise HTTPException(status_code=403, detail="forbidden")
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(str(target),
                        media_type=mimetypes.guess_type(target.name)[0] or "application/octet-stream")


@app.get("/health")
def health():
    return {"ok": True, "ts": int(time.time())}


@app.get("/")
@app.get("/index.html")
def serve_index():
    idx = STATIC_DIR / "index.html"
    if not idx.exists():
        raise HTTPException(status_code=404, detail="index.html missing")
    return FileResponse(str(idx))


@app.get("/{path:path}")
def serve_static(path: str):
    try:
        base = STATIC_DIR.resolve()
        target = (STATIC_DIR / path).resolve()
    except (OSError, ValueError):
        raise HTTPException(status_code=404, detail="not found")
    if not (target == base or target.is_relative_to(base)):
        raise HTTPException(status_code=403, detail="forbidden")
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(str(target))
