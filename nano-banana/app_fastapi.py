#!/usr/bin/env python3
"""FastAPI port of nano-banana's HTTP layer.

Coexists with the stdlib app.py: business functions are imported from there
as ``legacy``, only the HTTP dispatch is replaced. Cutover happens when this
version passes side-by-side parity tests.

Run with:
    DYLD_LIBRARY_PATH=/opt/homebrew/opt/expat/lib \
    DATA_DIR=$(pwd)/test-data PORT=8799 \
    ../.venv/bin/uvicorn app_fastapi:app --host 127.0.0.1 --port 8799

DYLD_LIBRARY_PATH is required on macOS to load Homebrew's expat; without it
xml.parsers.expat fails and cascades into any import that touches XML.
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

# Import the sibling stdlib app as a module. Business logic stays there;
# only routing/serialization is replaced.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
import app as legacy  # nano-banana/app.py

# Pillow imports are optional at runtime: thumbnails and EXIF strip fall back
# to raw bytes if Pillow is missing (e.g. when the venv is uninstalled).
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

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, Response
from starlette.datastructures import UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response as FastResponse


MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(200 * 1024 * 1024)))
STATIC_DIR = legacy.STATIC_DIR


app = FastAPI(
    title="nano-banana",
    version="2.0.0-fastapi",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


# ------------------------- middleware -------------------------------


@app.middleware("http")
async def _security_and_size(request: Request, call_next):
    """Stage 1c (nosniff) + stage 1d (200 MB upload cap) in one middleware."""
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


# ------------------------- handler shim -----------------------------


class _HandlerShim:
    """Minimal duck-type of SimpleHTTPRequestHandler for legacy helpers.

    ``.path`` and ``._raw_path`` include the query string so
    ``legacy._workspace_id`` recovers ``?ws=`` correctly. ``.client_address``
    is a 2-tuple like the real handler so ``_is_local`` works."""

    def __init__(self, request: Request):
        self.headers = request.headers
        client = request.client
        self.client_address = (client.host if client else "127.0.0.1", 0)
        qs = str(request.url.query or "")
        p = request.url.path
        self.path = f"{p}?{qs}" if qs else p
        self._raw_path = self.path


def _ws_id(request: Request) -> str:
    """Match stdlib nano-banana._workspace_id: header first, then query, sanitized."""
    import re as _re
    ws = (request.headers.get("X-Workspace-Id") or "").strip()
    if ws:
        return _re.sub(r"[^a-zA-Z0-9_\-]", "_", ws)[:64]
    ws = (request.query_params.get("ws") or "").strip()
    if ws:
        return _re.sub(r"[^a-zA-Z0-9_\-]", "_", ws)[:64]
    return "localhost"


def _is_local_req(request: Request) -> bool:
    ip = (request.headers.get("X-Forwarded-For") or (request.client.host if request.client else "") or "").strip()
    return ip in ("127.0.0.1", "::1", "localhost")


def _sniff_first_upload(files: dict[str, tuple[str, bytes]]) -> tuple[str | None, tuple[str, bytes] | None]:
    """Return (field_name, (filename, blob)) for the first non-empty upload."""
    for k, v in files.items():
        if v and v[1]:
            return k, v
    return None, None


def _api_error_resp(code: str, msg: str, detail: str = "", status: int = 400) -> JSONResponse:
    return JSONResponse(status_code=status, content=legacy.api_error(code, msg, detail))


# ------------------------- image pipeline ---------------------------


def _strip_exif_if_image(blob: bytes, mime: str) -> bytes:
    """Re-encode via Pillow to drop EXIF/GPS tags (privacy). No-op if Pillow
    missing or the format isn't a lossy container that carries EXIF."""
    if not _PIL_OK or Image is None:
        return blob
    if mime not in ("image/jpeg", "image/heic", "image/heif", "image/tiff", "image/webp"):
        return blob
    try:
        im = Image.open(io.BytesIO(blob))
        buf = io.BytesIO()
        # Preserve format but drop EXIF chunks by not passing exif= kwarg.
        # convert('RGB') for JPEG in case of RGBA/P input; leave WebP alone.
        fmt = im.format or "JPEG"
        if fmt == "JPEG" and im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        im.save(buf, format=fmt, quality=92, optimize=False)
        return buf.getvalue()
    except Exception:
        # If Pillow can't parse it (or a plugin's missing), keep the original.
        return blob


def _make_thumbnail_webp(path: Path, max_w: int) -> bytes | None:
    """Return WebP-encoded thumbnail bytes at max_w wide (keeps aspect), or
    None if Pillow is unavailable/decode fails. Caller falls back to full file."""
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
        "app": "nano-banana",
        "version": "2.0.0-fastapi",
        "port": int(os.environ.get("PORT", "8797")),
        "capabilities": ["text2image", "image2image"],
        "status": "ready",
    }


@app.get("/api/config")
def api_config():
    providers, config_error = legacy.load_provider_config()
    key = legacy.load_default_key()
    return {
        "ok": config_error is None,
        "providers": providers.get("providers", {}),
        "default_provider": providers.get("default_provider"),
        "has_key": bool(key),
        "masked_key": legacy.mask_key(key) if key else "",
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
    return legacy.activity_list(sees_all=sees_all, username=username)


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
    item = legacy.read_preset(ws).get("media", {}).get(field)
    stored_name = Path(item.get("stored", "")).name if item else ""
    if not item or not stored_name:
        raise HTTPException(status_code=404, detail="media not found")
    path = legacy._ws_media_dir(ws) / stored_name
    if not path.exists():
        raise HTTPException(status_code=404, detail="media not found")
    mime = item.get("mime") or "image/png"
    return FileResponse(str(path), media_type=mime, headers={"Cache-Control": "no-store"})


@app.get("/api/media/{stored}")
def api_media_stored(stored: str, request: Request, w: int | None = Query(None, ge=32, le=4096)):
    """Serve a workspace media file, optionally as a max-width WebP thumbnail.

    ``?w=256`` returns a Pillow-generated thumbnail (WebP, keeps aspect).
    Falls back to the raw file if Pillow can't decode or isn't installed."""
    ws = _ws_id(request)
    name = Path(urllib.parse.unquote(stored)).name  # collapse traversal
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
        media_type=mimetypes.guess_type(path.name)[0] or "image/png",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/jobs")
def api_jobs_list(request: Request):
    sees_all, username = legacy._view_scope(_HandlerShim(request))
    items: list[dict[str, Any]] = []
    with legacy.LOCK:
        for jid, j in legacy.JOBS.items():
            if not sees_all and j.get("username", "") != username:
                continue
            results = []
            for r in (j.get("results") or []):
                images = []
                for img in (r.get("images") or []):
                    if img.get("download_url"):
                        images.append({
                            "download_url": img.get("download_url", ""),
                            "filename": img.get("filename", ""),
                        })
                results.append({
                    "index": r.get("index", ""),
                    "task_id": r.get("task_id", ""),
                    "status": r.get("status", ""),
                    "images": images,
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
                "workspace_id": j.get("workspace_id", ""),
                "results": results,
                "errors": j.get("errors", []),
                "done": j.get("done", 0),
                "total": j.get("total", 0),
            })
    items.sort(key=lambda it: (it.get("submitted_at") or 0), reverse=True)
    return {"ok": True, "jobs": items}


@app.get("/api/jobs/{job_id}")
def api_job_detail(job_id: str):
    with legacy.LOCK:
        job = legacy.JOBS.get(job_id)
        data = json.loads(json.dumps(job)) if job else None
    if not data:
        raise HTTPException(status_code=404, detail="job not found")
    return data


@app.get("/api/download/{token}")
def api_download(token: str):
    with legacy.LOCK:
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
async def api_open_output_dir(request: Request, output_dir: str = Form("")):
    if not _is_local_req(request):
        return {"remote": True}
    try:
        return {"ok": True, "path": legacy.open_output_dir(output_dir)}
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
    field_name, item = _sniff_first_upload(files)
    if not field_name or not item:
        raise HTTPException(status_code=400, detail="no file provided")
    filename, data = item
    if not data:
        raise HTTPException(status_code=400, detail="empty file")
    if not legacy.sniff_is_image(data[:16]):
        return JSONResponse(status_code=415, content={
            "ok": False,
            "error": "uploaded file is not a recognized image format",
        })
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    # Strip EXIF/GPS before persisting. HEIC + JPEG are the common phone
    # uploads that carry location metadata.
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
        api_key = str(values.get("api_key") or legacy.load_default_key()).strip()
        if not api_key and not payload.get("dry_run"):
            return JSONResponse(status_code=400, content=legacy.api_error("invalid_request", "API key is required"))
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
                "title": str(values.get("prompt") or "")[:80] or "Nano Banana dry run",
                "request": legacy.summarize_payload(payload),
                "response": response,
            })
            return response
        request_data = {
            "raw": legacy.summarize_payload(payload),
            "parsed": legacy.summarize_values_files(values, files),
        }
        ws = _ws_id(request)
        username = legacy._decode_username(_HandlerShim(request))
        job_id = legacy.create_job(values, files, "api", "json", request_data, ws, username=username)
        return legacy.job_id_response(job_id)
    except Exception as exc:
        return JSONResponse(status_code=400, content=legacy.api_error("invalid_request", str(exc)))


@app.post("/api/jobs")
async def api_jobs_create(request: Request):
    values, files = await _parse_multipart(request)
    api_key = (values.get("api_key") or "") or legacy.load_default_key()
    if not api_key:
        return JSONResponse(status_code=400, content=legacy.api_error("invalid_request", "API key is required"))
    # Drop keys pointing at file fields (already extracted).
    submit_values = {k: v for k, v in values.items() if k not in files}
    submit_values["api_key"] = api_key
    submit_files = {k: v for k, v in files.items() if v and v[1]}
    request_data = legacy.summarize_values_files(submit_values, submit_files)
    ws = _ws_id(request)
    username = legacy._decode_username(_HandlerShim(request))
    job_id = legacy.create_job(
        submit_values, submit_files, "page", "multipart", request_data, ws, username=username,
    )
    return legacy.job_id_response(job_id)


# ------------------------- multipart helper -------------------------


class _FormShim:
    """Duck-type for legacy.get_field / get_file / collect_* helpers, which
    treat form as an object with keys()/getattr(item, filename)/item.file.read()."""

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


class _ValueItem:
    filename = None

    def __init__(self, value: str):
        self.value = value


class _FileItem:
    def __init__(self, filename: str, blob: bytes):
        self.filename = filename
        self.file = io.BytesIO(blob)


async def _parse_multipart(request: Request) -> tuple[dict[str, str], dict[str, tuple[str, bytes]]]:
    """Uniformly parse multipart/form-data via Starlette's form parser.

    Returns (values, files) where files maps field-name → (filename, bytes),
    matching what cgi.FieldStorage's downstream consumers expect."""
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


# ------------------------- health -----------------------------------


@app.get("/health")
def health():
    return {"ok": True, "ts": int(time.time())}


# ------------------------- static / SPA fallback --------------------
# NB: catch-all must be registered last, otherwise it grabs /health etc.


@app.get("/")
@app.get("/index.html")
def serve_index():
    idx = STATIC_DIR / "index.html"
    if not idx.exists():
        raise HTTPException(status_code=404, detail="index.html missing")
    return FileResponse(str(idx))


@app.get("/{path:path}")
def serve_static(path: str):
    # Path-traversal guard: reject anything that resolves outside STATIC_DIR.
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
