#!/usr/bin/env python3
"""FastAPI port of seedance's HTTP layer.

Parallel to app.py. See nano-banana/app_fastapi.py for the design rationale —
same shim strategy, same middleware, same free ETag/Range via Starlette's
FileResponse.

Extras vs nano-banana:
- /api/refmedia/{token}: anonymous, short-lived asset URLs served for Ark
- /api/optimize-prompt: DeepSeek call to rewrite user prompts
- sniff_kind(): image / video / audio all supported (ref_image_/ref_video_/ref_audio_)

Run:
    DYLD_LIBRARY_PATH=/opt/homebrew/opt/expat/lib \
    DATA_DIR=$(pwd)/test-data PORT=8788 \
    ../.venv/bin/uvicorn app_fastapi:app --host 127.0.0.1 --port 8788
"""
from __future__ import annotations

import io
import json
import mimetypes
import os
import shutil
import sys
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
import app as legacy  # seedance/app.py

try:
    from PIL import Image
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except ImportError:
        pillow_heif = None  # type: ignore
    _PIL_OK = True
except ImportError:
    Image = None  # type: ignore
    pillow_heif = None  # type: ignore
    _PIL_OK = False

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response as FastResponse
from starlette.datastructures import UploadFile


MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(200 * 1024 * 1024)))
STATIC_DIR = legacy.STATIC_DIR


app = FastAPI(
    title="seedance",
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
                content={
                    "ok": False,
                    "error": f"upload too large: {n} bytes (limit {MAX_UPLOAD_BYTES})",
                },
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


# ------------------------- shims ------------------------------------


class _HandlerShim:
    def __init__(self, request: Request):
        self.headers = request.headers
        client = request.client
        self.client_address = (client.host if client else "127.0.0.1", 0)
        qs = str(request.url.query or "")
        p = request.url.path
        self.path = f"{p}?{qs}" if qs else p
        self._raw_path = self.path


class _ValueItem:
    filename = None
    def __init__(self, value: str):
        self.value = value


class _FileItem:
    def __init__(self, filename: str, blob: bytes):
        self.filename = filename
        self.file = io.BytesIO(blob)


class _FormShim:
    def __init__(self, values: dict[str, str], files: dict[str, tuple[str, bytes]]):
        self._values = values
        self._files = files

    def keys(self):
        seen = set()
        for k in self._values:
            seen.add(k)
            yield k
        for k in self._files:
            if k not in seen:
                yield k

    def __getitem__(self, key: str):
        if key in self._files:
            filename, blob = self._files[key]
            return _FileItem(filename, blob)
        return _ValueItem(self._values.get(key, ""))

    def __contains__(self, key: str):
        return key in self._values or key in self._files


async def _parse_multipart(request: Request) -> tuple[dict[str, str], dict[str, tuple[str, bytes]]]:
    values: dict[str, str] = {}
    files: dict[str, tuple[str, bytes]] = {}
    form = await request.form()
    for key, val in form.multi_items():
        if isinstance(val, UploadFile):
            data = await val.read()
            filename = val.filename or key
            if data:
                files[key] = (filename, data)
        else:
            values[key] = str(val)
    return values, files


def _ws_id(request: Request) -> str:
    """Match stdlib seedance._workspace_id: header first, then query, sanitized."""
    import re as _re
    ws = (request.headers.get("X-Workspace-Id") or "").strip()
    if ws:
        return _re.sub(r"[^a-zA-Z0-9_\-]", "_", ws)[:64]
    ws = (request.query_params.get("ws") or "").strip()
    if ws:
        return _re.sub(r"[^a-zA-Z0-9_\-]", "_", ws)[:64]
    return "localhost"


def _job_created_response(job_id: str) -> JSONResponse:
    """Return the standard job-creation body AND surface job_id on the
    X-Job-Id response header. Portal's _proxy only registers usage stats
    (and thus counts seconds) when this header is present — the stdlib
    json_response sets it, but returning a bare dict from FastAPI does not,
    which silently dropped all seedance usage tracking after the FastAPI
    engine cutover. See legacy json_response for the header contract."""
    body = legacy.job_id_response(job_id)
    headers = {"Access-Control-Expose-Headers": "X-Job-Id"}
    jid = body.get("job_id") or body.get("id")
    if jid:
        headers["X-Job-Id"] = str(jid)
    return JSONResponse(content=body, headers=headers)


def _is_local_req(request: Request) -> bool:
    ip = (request.headers.get("X-Forwarded-For") or (request.client.host if request.client else "") or "").strip()
    return ip in ("127.0.0.1", "::1", "localhost")


def _strip_exif_if_image(blob: bytes, mime: str) -> bytes:
    if not _PIL_OK or Image is None:
        return blob
    if mime not in ("image/jpeg", "image/heic", "image/heif", "image/tiff", "image/webp"):
        return blob
    try:
        im = Image.open(io.BytesIO(blob))
        buf = io.BytesIO()
        fmt = im.format or "JPEG"
        if fmt == "JPEG" and im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        im.save(buf, format=fmt, quality=92, optimize=False)
        return buf.getvalue()
    except Exception:
        return blob


def _make_thumbnail_webp(path: Path, max_w: int) -> bytes | None:
    if not _PIL_OK or Image is None:
        return None
    try:
        im = Image.open(path)
        im.thumbnail((max_w, max_w * 10), Image.Resampling.LANCZOS)
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGB")
        buf = io.BytesIO()
        im.save(buf, format="WEBP", quality=82, method=4)
        return buf.getvalue()
    except Exception:
        return None


# ------------------------- read-only endpoints ----------------------


@app.get("/api/v1/meta")
def api_meta():
    return {
        "app": "seedance",
        "version": "2.0.0-fastapi",
        "port": int(os.environ.get("PORT", "8787")),
        "capabilities": ["text2video", "image2video", "frames2video", "multimodal2video"],
        "status": "ready",
    }


@app.get("/api/config")
def api_config():
    providers, config_error = legacy.load_provider_config()
    return {
        "ok": config_error is None,
        "providers": providers.get("providers", {}),
        "default_provider": providers.get("default_provider"),
        "config_error": config_error,
    }


@app.get("/api/request-template")
def api_request_template():
    return legacy.request_template()


@app.get("/api/schema")
def api_schema():
    return legacy.api_schema()


@app.get("/api/preset")
def api_preset_get(request: Request):
    return legacy.preset_for_client(_ws_id(request))


@app.get("/api/archives")
def api_archives(request: Request):
    return {"archives": legacy.list_archives(_HandlerShim(request))}


@app.get("/api/activity")
def api_activity(request: Request):
    sees_all, username = legacy._view_scope(_HandlerShim(request))
    return legacy.activity_list(ws_id=_ws_id(request), show_all=sees_all, username=username)


@app.get("/api/activity/{activity_id}")
def api_activity_detail(activity_id: str, request: Request):
    ws = _ws_id(request)
    record = next(
        (item for item in legacy.read_activity_log() if item.get("id") == activity_id),
        None,
    )
    is_admin = legacy._is_admin(_HandlerShim(request))
    if record and record.get("workspace_id") != ws and not is_admin:
        record = None
    body = legacy.activity_record_for_client(record) or {"error": "activity not found"}
    return JSONResponse(status_code=200 if record else 404, content=body)


@app.get("/api/default-output-dir")
def api_default_output_dir():
    return {"path": legacy.desktop_output_dir()}


@app.get("/api/preset-media/{field}")
def api_preset_media(field: str, request: Request):
    ws = _ws_id(request)
    preset = legacy.read_preset(ws)
    item = preset.get("media", {}).get(field)
    stored_name = Path(item.get("stored", "")).name if item else ""
    if not item or not stored_name:
        raise HTTPException(status_code=404, detail="media not found")
    path = legacy._ws_media_dir(ws) / stored_name
    if not path.exists():
        raise HTTPException(status_code=404, detail="media not found")
    mime = item.get("mime") or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(str(path), media_type=mime, headers={"Cache-Control": "no-store"})


@app.get("/api/media/{stored}")
def api_media_stored(stored: str, request: Request, w: int | None = Query(None, ge=32, le=4096)):
    ws = _ws_id(request)
    name = Path(urllib.parse.unquote(stored)).name
    path = legacy._ws_media_dir(ws) / name
    if not path.exists():
        raise HTTPException(status_code=404, detail="media not found")
    if w is not None:
        thumb = _make_thumbnail_webp(path, w)
        if thumb is not None:
            return FastResponse(
                content=thumb,
                media_type="image/webp",
                headers={"Cache-Control": "public, max-age=3600"},
            )
    return FileResponse(
        str(path),
        media_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/refmedia/{token_ext}")
def api_refmedia(token_ext: str):
    """Public anonymous endpoint that lets Ark fetch reference media via a
    short-lived signed token. Not auth-guarded because Ark's fetcher has no
    session cookie."""
    token = token_ext.split(".", 1)[0]
    with legacy.REFMEDIA_LOCK:
        meta = legacy.REFMEDIA.get(token)
    if not meta or meta.get("expires_at", 0) < time.time() or not meta["path"].exists():
        return FastResponse(content=b"", status_code=404)
    path = meta["path"]
    return FileResponse(
        str(path),
        media_type=meta.get("mime") or "application/octet-stream",
        headers={
            "Cache-Control": "public, max-age=3600",
            "Access-Control-Allow-Origin": "*",
        },
    )


@app.get("/api/jobs")
def api_jobs_list(request: Request):
    sees_all, username = legacy._view_scope(_HandlerShim(request))
    items: list[dict[str, Any]] = []
    with legacy.JOBS_LOCK:
        for jid, j in legacy.JOBS.items():
            if not sees_all and j.get("username", "") != username:
                continue
            results = []
            for r in (j.get("results") or []):
                results.append({
                    "download_url": r.get("download_url", ""),
                    "filename": r.get("filename", ""),
                    "index": r.get("index", ""),
                    "task_id": r.get("task_id", ""),
                })
            items.append({
                "job_id": jid,
                "status": j.get("status", "pending"),
                "model": j.get("model", ""),
                "prompt": (j.get("params", {}).get("prompt") or j.get("prompt", ""))[:200],
                "created_at": j.get("created_at", ""),
                "submitted_at": j.get("submitted_at"),
                "started_at": j.get("started_at"),
                "finished_at": j.get("finished_at"),
                "username": j.get("username", ""),
                "results": results,
                "errors": j.get("errors", []),
                "done": j.get("done", 0),
                "workspace_id": j.get("workspace_id", ""),
                "total": j.get("total", 0),
            })
    items.sort(key=lambda it: (it.get("submitted_at") or 0), reverse=True)
    return {"ok": True, "jobs": items}


@app.get("/api/jobs/{job_id}")
def api_job_detail(job_id: str):
    with legacy.JOBS_LOCK:
        job = legacy.JOBS.get(job_id)
        data = json.loads(json.dumps(job)) if job else None
    if not data:
        raise HTTPException(status_code=404, detail="job not found")
    return data


@app.get("/api/download/{token}")
def api_download(token: str):
    with legacy.JOBS_LOCK:
        path = legacy.FILES.get(token) if hasattr(legacy, "FILES") else None
    if not path or not path.exists():
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(
        str(path),
        filename=path.name,
        media_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
    )


# ------------------------- write endpoints --------------------------


@app.post("/api/choose-output-dir")
def api_choose_output_dir(request: Request):
    if not _is_local_req(request):
        return {"remote": True}
    try:
        return {"path": legacy.choose_output_dir()}
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@app.post("/api/open-output-dir")
async def api_open_output_dir(request: Request):
    if not _is_local_req(request):
        return {"remote": True}
    values, _files = await _parse_multipart(request)
    try:
        return {"ok": True, "path": legacy.open_output_dir(values.get("output_dir", ""))}
    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content=legacy.api_error("open_output_dir_failed", "打开输出目录失败", str(exc)),
        )


@app.post("/api/cleanup-cache")
def api_cleanup_cache(request: Request):
    if not _is_local_req(request):
        return {"remote": True}
    try:
        return legacy.cleanup_cache()
    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content=legacy.api_error("cleanup_cache_failed", "清理缓存失败", str(exc)),
        )


@app.post("/api/workspace/snapshot")
async def api_workspace_snapshot(request: Request):
    ws = _ws_id(request)
    values, files = await _parse_multipart(request)
    try:
        snap = legacy.collect_workspace_snapshot_from_form(_FormShim(values, files), ws)
        return legacy.preset_to_client(snap, ws)
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})


@app.post("/api/optimize-prompt")
async def api_optimize_prompt(request: Request):
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"ok": False, "error": "请求格式异常"})
    return legacy.optimize_prompt(data.get("prompt", ""))


@app.post("/api/preset")
async def api_preset_post(request: Request):
    ws = _ws_id(request)
    values, files = await _parse_multipart(request)
    preset = legacy.collect_preset_from_form(_FormShim(values, files), ws)
    legacy.write_active_preset(preset, ws)
    archive_name = (values.get("archive_name") or "").strip()
    archive = legacy.save_archive_file(archive_name, preset, ws).name if archive_name else None
    data = legacy.preset_for_client(ws)
    data["archive"] = archive
    data["archives"] = legacy.list_archives(_HandlerShim(request))
    return data


@app.post("/api/media/upload")
async def api_media_upload(request: Request):
    ws = _ws_id(request)
    ctype = request.headers.get("Content-Type", "")
    if not ctype.startswith("multipart/form-data"):
        raise HTTPException(status_code=400, detail="expected multipart/form-data")
    values, files = await _parse_multipart(request)
    field_name = None
    item = None
    for k, v in files.items():
        if v and v[1]:
            field_name = k
            item = v
            break
    if not field_name or not item:
        raise HTTPException(status_code=400, detail="no file provided")
    filename, data = item
    if not data:
        raise HTTPException(status_code=400, detail="empty file")
    # Field-name prefix declares the kind; magic bytes must match.
    expected_kind = None
    if field_name.startswith("ref_image_"):
        expected_kind = "image"
    elif field_name.startswith("ref_video_"):
        expected_kind = "video"
    elif field_name.startswith("ref_audio_"):
        expected_kind = "audio"
    if expected_kind:
        actual = legacy.sniff_kind(data[:16])
        if actual != expected_kind:
            return JSONResponse(status_code=415, content={
                "ok": False,
                "error": f"content does not match {expected_kind}: detected {actual or 'unknown'}",
            })
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    # EXIF strip for image uploads (video/audio pass through).
    data = _strip_exif_if_image(data, mime)
    suffix = Path(filename).suffix.lower()
    stored = f"{uuid.uuid4().hex}_{field_name}{suffix}"
    media_dir = legacy._ws_media_dir(ws)
    media_dir.mkdir(parents=True, exist_ok=True)
    (media_dir / stored).write_bytes(data)
    preset = legacy.read_preset(ws)
    media = preset.get("media") or {}
    old = media.get(field_name)
    if old and old.get("stored"):
        old_path = media_dir / old["stored"]
        try:
            if old_path.exists() and old_path.name != stored:
                old_path.unlink()
        except Exception:
            pass
    media[field_name] = {"filename": filename, "mime": mime, "stored": stored}
    preset["media"] = media
    legacy.write_active_preset(preset, ws)
    url = f"/api/media/{urllib.parse.quote(stored)}?ws={urllib.parse.quote(ws)}&v={int(time.time())}"
    return {
        "ok": True,
        "field": field_name,
        "filename": filename,
        "mime": mime,
        "stored": stored,
        "url": url,
    }


@app.post("/api/archive/load")
async def api_archive_load(request: Request):
    values, _files = await _parse_multipart(request)
    try:
        data = legacy.load_archive_file(values.get("archive_name", ""), _HandlerShim(request))
        data["archives"] = legacy.list_archives(_HandlerShim(request))
        return data
    except Exception as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})


@app.post("/api/archive/delete")
async def api_archive_delete(request: Request):
    if not _is_local_req(request):
        return JSONResponse(status_code=403, content={"ok": False, "error": "admin only"})
    values, _files = await _parse_multipart(request)
    path = legacy.archive_path(values.get("archive_name", ""), _ws_id(request))
    if path.exists():
        path.unlink()
    return {"archives": legacy.list_archives(_HandlerShim(request))}


@app.post("/api/preset/clear")
def api_preset_clear(request: Request):
    if not _is_local_req(request):
        return JSONResponse(status_code=403, content={"ok": False, "error": "admin only"})
    ws = _ws_id(request)
    ws_dir = legacy.STATE_DIR / "workspaces" / ws
    if ws_dir.exists():
        shutil.rmtree(ws_dir)
    return {"ok": True}


@app.post("/api/jobs/json")
async def api_jobs_json(request: Request):
    try:
        payload = await request.json()
    except Exception as exc:
        return JSONResponse(status_code=400, content=legacy.api_error("invalid_request", str(exc)))
    try:
        values, files = legacy.values_files_from_json(payload)
        # Provider is locked to volcengine — always inject the company key.
        values["provider"] = "volcengine"
        api_key = legacy.SECRETS.get("volcengine_api_key", "")
        if not api_key and not payload.get("dry_run"):
            return JSONResponse(status_code=400,
                                content=legacy.api_error("invalid_request", "API key is required"))
        if api_key:
            values["api_key"] = api_key
        if payload.get("dry_run"):
            response = {
                "ok": True,
                "dry_run": True,
                "values": {k: ("***" if k == "api_key" else v) for k, v in values.items()},
                "files": {k: {"filename": v[0], "bytes": len(v[1])} for k, v in files.items()},
            }
            legacy.record_activity({
                "source": "api",
                "request_kind": "json_dry_run",
                "status": "succeeded",
                "title": str(values.get("prompt") or "")[:80] or "Seedance dry run",
                "request": legacy.summarize_payload(payload),
                "response": response,
                "username": legacy._decode_username(_HandlerShim(request)),
            })
            return response
        request_data = {
            "raw": legacy.summarize_payload(payload),
            "parsed": legacy.summarize_values_files(values, files),
        }
        ws = _ws_id(request)
        username = legacy._decode_username(_HandlerShim(request))
        job_id = legacy.create_job(values, files, "api", "json", request_data, ws, username=username)
        return _job_created_response(job_id)
    except Exception as exc:
        return JSONResponse(status_code=400, content=legacy.api_error("invalid_request", str(exc)))


@app.post("/api/jobs")
async def api_jobs_create(request: Request):
    values, files = await _parse_multipart(request)
    # Provider is locked to volcengine — always use the company SECRETS key,
    # ignore client-supplied api_key/provider. Matches stdlib do_POST behaviour.
    api_key = legacy.SECRETS.get("volcengine_api_key", "")
    if not api_key:
        return JSONResponse(status_code=400,
                            content=legacy.api_error("invalid_request", "API key is required"))
    submit_values = {k: v for k, v in values.items() if k not in files}
    submit_values["api_key"] = api_key
    submit_values["provider"] = "volcengine"
    submit_files = {k: v for k, v in files.items() if v and v[1]}
    request_data = legacy.summarize_values_files(submit_values, submit_files)
    ws = _ws_id(request)
    username = legacy._decode_username(_HandlerShim(request))
    job_id = legacy.create_job(
        submit_values, submit_files, "page", "multipart", request_data, ws, username=username,
    )
    return _job_created_response(job_id)


# ------------------------- health + static --------------------------


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


@app.get("/outputs/{path:path}")
def serve_output(path: str):
    """Legacy standalone-frontend downloads. Portal uses /api/download/{token}."""
    try:
        base = legacy.OUTPUT_DIR.resolve()
        target = (legacy.OUTPUT_DIR / path).resolve()
    except (OSError, ValueError):
        raise HTTPException(status_code=404, detail="not found")
    if not (target == base or target.is_relative_to(base)):
        raise HTTPException(status_code=403, detail="forbidden")
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(str(target))


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
