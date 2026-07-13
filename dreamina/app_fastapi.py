#!/usr/bin/env python3
"""FastAPI port of dreamina's HTTP layer.

dreamina has 46 endpoints (env/CLI wrappers, 8 task types, account CRUD,
history, archive, preset, output-dir chooser). All handler logic lives in
instance methods of Handler(SimpleHTTPRequestHandler). Instead of hand-
porting all 46, we bridge: each FastAPI route builds a fake handler that
exposes headers/path/rfile/wfile/send_response/end_headers/send_error,
invokes the legacy method as an unbound function, then turns the captured
wfile bytes into a FastAPI Response.

Preserves all instance methods (including the SHA-256-verified install-cli,
subprocess CLI calls, JSON-and-multipart dual parsing, ETag/Range in
serve_file) unchanged.

Run:
    DYLD_LIBRARY_PATH=/opt/homebrew/opt/expat/lib \
    DATA_DIR=$(pwd)/test-data PORT=8890 \
    ../.venv/bin/uvicorn app_fastapi:app --host 127.0.0.1 --port 8890
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
import app as legacy

# stdlib app.py initializes EXECUTOR / dirs / migrations inside main(), which
# we don't call. Do the same setup here so handle_generate can dispatch to
# EXECUTOR.submit() without hitting None.
import concurrent.futures
if legacy.EXECUTOR is None:
    try:
        legacy.ensure_dirs()
    except Exception:
        pass
    try:
        legacy.cleanup_old_uploads()
    except Exception:
        pass
    try:
        legacy.migrate_default_account()
    except Exception:
        pass
    _cfg = legacy.load_config()
    legacy.EXECUTOR = concurrent.futures.ThreadPoolExecutor(
        max_workers=_cfg.get("max_concurrent", 5)
    )

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response as FastResponse, StreamingResponse


MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(200 * 1024 * 1024)))
STATIC_DIR = legacy.STATIC_DIR


app = FastAPI(
    title="dreamina",
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
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "X-Workspace-Id", "X-Api-Key"],
    )


# ------------------------- Legacy handler bridge --------------------


class _LegacyBridge:
    """Fake handler that satisfies dreamina's Handler.handle_*() methods.

    Exposes the same surface as _LegacyBridge in volcengine's port, plus
    the two attrs the parse_multipart helper reads (rfile + Content-Length
    via .headers) and streaming write support for SSE (install-cli logs
    are streamed line-by-line through wfile.write + wfile.flush).

    __getattr__ falls back to legacy.Handler for any method we haven't
    stubbed — this is what lets handle_generate(self) call self.build_cli_args(...)
    or self._decode_username() unchanged: the attribute is fetched off the
    legacy Handler class and rebound to *this* bridge instance so `self`
    references still work correctly."""

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

    def __getattr__(self, name: str):
        # Called only when the normal attribute lookup fails. Any Handler
        # method we haven't overridden bounces here — grab it off the class
        # and rebind to this bridge so `self` inside it points at us.
        try:
            attr = getattr(legacy.Handler, name)
        except AttributeError:
            raise AttributeError(f"'_LegacyBridge' object has no attribute {name!r}") from None
        if callable(attr):
            # Bind the unbound function/method to this instance
            return attr.__get__(self, type(self))
        return attr

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
            if k.lower() in ("content-length", "server", "date", "connection"):
                continue
            headers[k] = v
        content = self._body.getvalue()
        media_type = headers.pop("Content-Type", None) or headers.pop("content-type", None)
        return FastResponse(content=content, status_code=self._status,
                            headers=headers, media_type=media_type)


async def _bridge_method(request: Request, method_name: str, *args, **kwargs) -> Response:
    """Invoke legacy.Handler.<method_name> as an unbound function, passing
    a _LegacyBridge as the self argument."""
    body = await request.body()
    bridge = _LegacyBridge(request, body)
    fn = getattr(legacy.Handler, method_name)
    try:
        fn(bridge, *args, **kwargs)
    except Exception as exc:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)})
    return bridge.to_response()


# ------------------------- meta / env / CLI ------------------------


@app.get("/api/v1/meta")
async def api_meta(request: Request):
    return await _bridge_method(request, "handle_meta")


@app.get("/api/env/check")
async def api_env_check(request: Request):
    return await _bridge_method(request, "handle_env_check")


@app.get("/api/env/login-poll")
async def api_env_login_poll(request: Request):
    return await _bridge_method(request, "handle_login_poll")


@app.post("/api/env/login")
async def api_env_login(request: Request):
    return await _bridge_method(request, "handle_login")


@app.post("/api/env/login-cancel")
async def api_env_login_cancel(request: Request):
    return await _bridge_method(request, "handle_login_cancel")


@app.post("/api/env/switch-account")
async def api_env_switch_account(request: Request):
    return await _bridge_method(request, "handle_switch_account")


@app.post("/api/env/update-cli")
async def api_env_update_cli(request: Request):
    return await _bridge_method(request, "handle_install_cli")


@app.post("/api/env/install-cli")
async def api_env_install_cli(request: Request):
    """install-cli streams SSE events. Consume the whole thing into memory
    (small; log-line rate is low) rather than plumb wfile as an async
    generator — matches how the legacy handler already writes it."""
    return await _bridge_method(request, "handle_install_cli")


# ------------------------- accounts ---------------------------------


@app.get("/api/accounts")
async def api_accounts(request: Request):
    return await _bridge_method(request, "handle_accounts_list")


@app.post("/api/accounts")
async def api_accounts_create(request: Request):
    return await _bridge_method(request, "handle_account_create")


@app.post("/api/accounts/repair-all")
async def api_accounts_repair(request: Request):
    return await _bridge_method(request, "handle_accounts_repair_all")


@app.post("/api/accounts/active")
async def api_accounts_set_active(request: Request):
    return await _bridge_method(request, "handle_set_active_account")


@app.get("/api/accounts/{acc_id}/login-poll")
async def api_account_login_poll(request: Request, acc_id: str):
    return await _bridge_method(request, "handle_account_login_poll", acc_id)


@app.post("/api/accounts/{acc_id}/login")
async def api_account_login(request: Request, acc_id: str):
    return await _bridge_method(request, "handle_account_login", acc_id)


@app.post("/api/accounts/{acc_id}/logout")
async def api_account_logout(request: Request, acc_id: str):
    return await _bridge_method(request, "handle_account_logout", acc_id)


@app.post("/api/accounts/{acc_id}/refresh")
async def api_account_refresh(request: Request, acc_id: str):
    return await _bridge_method(request, "handle_account_refresh", acc_id)


@app.post("/api/accounts/{acc_id}/delete")
async def api_account_delete(request: Request, acc_id: str):
    return await _bridge_method(request, "handle_account_delete", acc_id)


@app.post("/api/accounts/{acc_id}/rename")
async def api_account_rename(request: Request, acc_id: str):
    return await _bridge_method(request, "handle_account_rename", acc_id)


@app.post("/api/dispatch-mode")
async def api_dispatch_mode(request: Request):
    return await _bridge_method(request, "handle_set_dispatch_mode")


# ------------------------- generate (8 task types) ------------------


@app.post("/api/text2image")
async def api_text2image(request: Request):
    return await _bridge_method(request, "handle_generate", "text2image")


@app.post("/api/image2image")
async def api_image2image(request: Request):
    return await _bridge_method(request, "handle_generate", "image2image")


@app.post("/api/text2video")
async def api_text2video(request: Request):
    return await _bridge_method(request, "handle_generate", "text2video")


@app.post("/api/image2video")
async def api_image2video(request: Request):
    return await _bridge_method(request, "handle_generate", "image2video")


@app.post("/api/frames2video")
async def api_frames2video(request: Request):
    return await _bridge_method(request, "handle_generate", "frames2video")


@app.post("/api/multimodal2video")
async def api_multimodal2video(request: Request):
    return await _bridge_method(request, "handle_generate", "multimodal2video")


@app.post("/api/multiframe2video")
async def api_multiframe2video(request: Request):
    return await _bridge_method(request, "handle_generate", "multiframe2video")


# ------------------------- jobs / history / activity ----------------


@app.get("/api/jobs")
async def api_jobs(request: Request):
    return await _bridge_method(request, "handle_jobs_list")


@app.get("/api/jobs/{job_id}")
async def api_job(request: Request, job_id: str):
    return await _bridge_method(request, "handle_job_status", job_id)


@app.post("/api/jobs/{job_id}/retry")
async def api_job_retry(request: Request, job_id: str):
    return await _bridge_method(request, "handle_retry", job_id)


@app.get("/api/history")
async def api_history(request: Request):
    return await _bridge_method(request, "handle_history")


@app.get("/api/activity")
async def api_activity(request: Request):
    return await _bridge_method(request, "handle_activity_list")


@app.get("/api/activity/{activity_id}")
async def api_activity_detail(request: Request, activity_id: str):
    return await _bridge_method(request, "handle_activity_detail", activity_id)


# ------------------------- preset / archive ------------------------


@app.get("/api/preset")
async def api_preset_get(request: Request):
    # Legacy inline: json_response(handler, 200, {"ok": True, **preset_for_client(handler)})
    body = await request.body()
    bridge = _LegacyBridge(request, body)
    try:
        data = legacy.preset_for_client(bridge)
        return {"ok": True, **data}
    except Exception as exc:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)})


@app.post("/api/preset")
async def api_preset_post(request: Request):
    return await _bridge_method(request, "handle_preset_save")


@app.post("/api/preset/clear")
async def api_preset_clear(request: Request):
    return await _bridge_method(request, "handle_preset_clear")


@app.get("/api/preset-media/{field}")
async def api_preset_media(request: Request, field: str):
    return await _bridge_method(request, "handle_preset_media", field)


@app.get("/api/archives")
async def api_archives(request: Request):
    body = await request.body()
    bridge = _LegacyBridge(request, body)
    try:
        return {"ok": True, "archives": legacy.list_archives(bridge)}
    except Exception as exc:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)})


@app.post("/api/archive/load")
async def api_archive_load(request: Request):
    return await _bridge_method(request, "handle_archive_load")


@app.post("/api/archive/delete")
async def api_archive_delete(request: Request):
    return await _bridge_method(request, "handle_archive_delete")


@app.post("/api/archive/from-history")
async def api_archive_from_history(request: Request):
    return await _bridge_method(request, "handle_archive_from_history")


# ------------------------- output dir / cache -----------------------


@app.get("/api/default-output-dir")
def api_default_output_dir():
    return {"path": legacy.desktop_output_dir()}


@app.post("/api/choose-output-dir")
async def api_choose_output_dir(request: Request):
    ip = (request.headers.get("X-Forwarded-For") or (request.client.host if request.client else "") or "").strip()
    if ip not in ("127.0.0.1", "::1", "localhost"):
        return {"remote": True}
    try:
        return {"path": legacy.choose_output_dir()}
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@app.post("/api/open-output-dir")
async def api_open_output_dir(request: Request):
    ip = (request.headers.get("X-Forwarded-For") or (request.client.host if request.client else "") or "").strip()
    if ip not in ("127.0.0.1", "::1", "localhost"):
        return {"remote": True}
    body = await request.body()
    bridge = _LegacyBridge(request, body)
    ctype = request.headers.get("Content-Type", "")
    output_dir = ""
    if "multipart" in ctype:
        try:
            fields, _ = legacy.parse_multipart(bridge)
            output_dir = fields.get("output_dir", "")
        except Exception:
            pass
    else:
        try:
            data = await request.json()
            output_dir = data.get("output_dir", "")
        except Exception:
            pass
    try:
        return {"ok": True, "path": legacy.open_output_dir(output_dir)}
    except Exception as exc:
        return JSONResponse(status_code=500,
                            content={"ok": False, "error": str(exc)})


@app.post("/api/cache/clean")
async def api_cache_clean(request: Request):
    return await _bridge_method(request, "handle_cache_clean")


@app.post("/api/cleanup-cache")
async def api_cleanup_cache(request: Request):
    return await _bridge_method(request, "handle_cache_clean")


# ------------------------- serve outputs / uploads / static ---------


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
