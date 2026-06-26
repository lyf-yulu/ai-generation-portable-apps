#!/usr/bin/env python3
from __future__ import annotations

import base64
import cgi
import concurrent.futures
import json
import mimetypes
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
_DATA_BASE = Path(os.environ.get("DATA_DIR", str(ROOT)))
STATIC_DIR = ROOT / "static"
OUTPUT_DIR = _DATA_BASE / "outputs"
STATE_DIR = _DATA_BASE / "state"
MEDIA_DIR = STATE_DIR / "media"
PRESET_PATH = STATE_DIR / "preset.json"
ACTIVITY_PATH = STATE_DIR / "activity_log.json"
ARCHIVE_DIR = _DATA_BASE / "archives"
PROVIDERS_PATH = ROOT / "providers.json"


def _is_local(handler: SimpleHTTPRequestHandler) -> bool:
    ip = (handler.headers.get("X-Forwarded-For") or handler.client_address[0] or "").strip()
    return ip in ("127.0.0.1", "::1", "localhost")


def _workspace_id(handler) -> str:
    """Extract workspace_id: 1) X-Workspace-Id header  2) ?ws= query  3) localhost."""
    ws = (handler.headers.get("X-Workspace-Id") or "").strip()
    if ws:
        return re.sub(r"[^a-zA-Z0-9_\-]", "_", ws)[:64]
    # Fallback to query parameter
    qs = urllib.parse.urlparse(handler.path).query
    params = urllib.parse.parse_qs(qs)
    if "ws" in params:
        return re.sub(r"[^a-zA-Z0-9_\-]", "_", str(params["ws"][0]))[:64]
    return "localhost"


def _ws_dir(ws_id: str) -> Path:
    return STATE_DIR / "workspaces" / ws_id


def _ws_media_dir(ws_id: str) -> Path:
    return _ws_dir(ws_id) / "media"


def _ws_preset_path(ws_id: str) -> Path:
    return _ws_dir(ws_id) / "preset.json"


DEFAULT_BASE_URL = "https://ai.t8star.org"
DEFAULT_CONFIG = Path.home() / "ComfyUI/custom_nodes/Comfyui-zhenzhen/Comflyapi.json"
MAX_SEED = 2147483647

JOBS: dict[str, dict[str, Any]] = {}
FILES: dict[str, Path] = {}
LOCK = threading.Lock()
STATE_LOCK = threading.Lock()
ACTIVITY_LIMIT = 100


def _atomic_write(path: Path, content: str):
    """Thread-safe atomic write: tmp → rename."""
    with STATE_LOCK:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)

# ---- Client IP helpers ----

def _client_ip(handler: SimpleHTTPRequestHandler) -> str:
    xff = handler.headers.get("X-Forwarded-For", "").strip()
    if xff:
        ip = xff.split(",")[0].strip()
        if ip:
            return re.sub(r"[^0-9a-fA-F.:]+", "_", ip)
    addr = handler.client_address[0] if handler.client_address else "127.0.0.1"
    return re.sub(r"[^0-9a-fA-F.:]+", "_", addr)


def _archive_dir_for(handler_or_ip: Any) -> Path:
    if isinstance(handler_or_ip, str):
        return ARCHIVE_DIR / handler_or_ip
    return ARCHIVE_DIR / _client_ip(handler_or_ip)

FILE_FIELDS = {f"image_{i}" for i in range(1, 15)}
VALUE_FIELDS = {
    "api_key", "base_url", "output_dir", "provider", "mode", "model", "aspect_ratio", "image_size",
    "custom_model", "response_format", "seed", "control_after_generate", "skip_error", "repeat_count",
    "concurrency", "poll_interval", "timeout", "vary_seed", "prompt", "archive_name", "output_name",
    "resize_enabled", "resize_width", "resize_height", "resize_interpolation", "resize_method",
    "resize_condition", "resize_multiple_of",
}

FALLBACK_PROVIDERS = {
    "schema_version": 1,
    "app": "nano-banana",
    "default_provider": "t8star",
    "providers": {
        "t8star": {
            "label": "T8Star Images API",
            "base_url": DEFAULT_BASE_URL,
            "api_style": "openai_images",
            "defaults": {"mode": "img2img", "model": "nano-banana-2", "aspect_ratio": "auto", "image_size": "2K", "response_format": "url", "control_after_generate": "randomize", "repeat_count": 1, "concurrency": 1, "poll_interval": 10, "timeout": 900, "vary_seed": True, "resize_enabled": False, "resize_width": 1700, "resize_height": 2500, "resize_interpolation": "high", "resize_method": "stretch", "resize_condition": "always", "resize_multiple_of": 0},
            "models": [{"id": "nano-banana-2", "label": "nano-banana-2"}, {"id": "gemini-3.1-flash-image-preview", "label": "gemini-3.1-flash-image-preview"}],
        },
        "gemini": {
            "label": "Chiyun",
            "base_url": "https://chiyun.work",
            "api_style": "gemini_generate_content",
            "defaults": {"mode": "img2img", "model": "banana2-ssvip", "aspect_ratio": "auto", "image_size": "2K", "response_format": "url", "control_after_generate": "randomize", "repeat_count": 1, "concurrency": 1, "poll_interval": 10, "timeout": 900, "vary_seed": True, "resize_enabled": False, "resize_width": 1700, "resize_height": 2500, "resize_interpolation": "high", "resize_method": "stretch", "resize_condition": "always", "resize_multiple_of": 0},
            "models": [{"id": "banana2-ssvip", "label": "banana2-ssvip"}, {"id": "nano-banana2[2K]-base", "label": "nano-banana2[2K]-base"}, {"id": "gpt-image-2", "label": "gpt-image-2"}],
        },
    },
}


def json_response(handler: SimpleHTTPRequestHandler, status: int, data: dict[str, Any]) -> None:
    raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    # Surface job_id on a header so the upstream proxy can register usage stats
    # without buffering the body (P0 fix for #15 portal-wide hang).
    if isinstance(data, dict):
        jid = data.get("job_id") or data.get("id")
        if jid:
            handler.send_header("X-Job-Id", str(jid))
    if os.environ.get("CORS") == "1":
        handler.send_header("Access-Control-Allow-Origin", "*")
        handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        handler.send_header("Access-Control-Allow-Headers", "Content-Type")
        handler.send_header("Access-Control-Expose-Headers", "X-Job-Id")
    handler.end_headers()
    handler.wfile.write(raw)


def api_error(code: str, message: str, detail: str = "", retryable: bool = False) -> dict[str, Any]:
    return {
        "ok": False,
        "error": message,
        "error_detail": detail,
        "error_code": code,
        "error_info": {
            "code": code,
            "message": message,
            "detail": detail,
            "retryable": retryable,
        },
    }


def load_provider_config() -> tuple[dict[str, Any], dict[str, Any] | None]:
    try:
        config = json.loads(PROVIDERS_PATH.read_text(encoding="utf-8"))
        if config.get("schema_version") != 1 or config.get("app") != "nano-banana":
            raise ValueError("providers.json schema_version/app mismatch")
        if not isinstance(config.get("providers"), dict) or not config["providers"]:
            raise ValueError("providers.json providers must be a non-empty object")
        return config, None
    except Exception as exc:
        return FALLBACK_PROVIDERS, {
            "code": "provider_config_error",
            "message": "供应商配置读取失败，请联系维护者",
            "detail": str(exc),
            "retryable": False,
        }


def provider_defaults(config: dict[str, Any], provider: str, model: str = "") -> dict[str, Any]:
    providers = config.get("providers") or {}
    provider_cfg = providers.get(provider) or providers.get(config.get("default_provider")) or next(iter(providers.values()))
    defaults = dict(provider_cfg.get("defaults") or {})
    if provider_cfg.get("base_url"):
        defaults.setdefault("base_url", provider_cfg["base_url"])
    defaults.setdefault("provider", provider)
    models = provider_cfg.get("models") if isinstance(provider_cfg.get("models"), list) else []
    selected = model or str(defaults.get("model") or "")
    for item in models:
        if isinstance(item, dict) and item.get("id") == selected:
            defaults.update(item.get("defaults") or {})
            break
    return defaults


def now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def read_activity_log() -> list[dict[str, Any]]:
    if not ACTIVITY_PATH.exists():
        return []
    try:
        data = json.loads(ACTIVITY_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def write_activity_log(items: list[dict[str, Any]]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    content = json.dumps(items[-ACTIVITY_LIMIT:], ensure_ascii=False, indent=2)
    _atomic_write(ACTIVITY_PATH, content)


def summarize_media_item(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    result = {key: value for key, value in item.items() if key != "data_url"}
    if item.get("data_url"):
        result["data_url"] = True
        result["chars"] = len(str(item["data_url"]))
    return result


def summarize_payload(value: Any) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key == "api_key":
                result[key] = mask_key(str(item))
            elif key == "media" and isinstance(item, dict):
                result[key] = {name: summarize_media_item(media_item) for name, media_item in item.items()}
            else:
                result[key] = summarize_payload(item)
        return result
    if isinstance(value, list):
        return [summarize_payload(item) for item in value]
    if isinstance(value, str) and value.startswith("data:"):
        return {"data_url": True, "chars": len(value)}
    return value


def summarize_values_files(values: dict[str, Any], files: dict[str, tuple[str, bytes]]) -> dict[str, Any]:
    return {
        "values": {key: (mask_key(str(value)) if key == "api_key" else value) for key, value in values.items()},
        "files": {key: {"filename": item[0], "bytes": len(item[1])} for key, item in files.items()},
    }


def record_activity(record: dict[str, Any]) -> None:
    items = read_activity_log()
    record.setdefault("id", uuid.uuid4().hex)
    record.setdefault("created_at", now_text())
    record.setdefault("updated_at", record["created_at"])
    items.append(record)
    write_activity_log(items)


def update_activity(activity_id: str | None, **updates: Any) -> None:
    if not activity_id:
        return
    items = read_activity_log()
    for item in items:
        if item.get("id") == activity_id:
            item.update(updates)
            item["updated_at"] = now_text()
            write_activity_log(items)
            return


def activity_list() -> dict[str, Any]:
    items = read_activity_log()
    summary = []
    counts = {"total": len(items), "page": 0, "api": 0, "succeeded": 0, "failed": 0, "running": 0}
    for item in items:
        source = str(item.get("source") or "")
        status = str(item.get("status") or "")
        if source in counts:
            counts[source] += 1
        if status in counts:
            counts[status] += 1
        summary.append({
            "id": item.get("id"),
            "job_id": item.get("job_id"),
            "source": source,
            "status": status,
            "created_at": item.get("created_at"),
            "updated_at": item.get("updated_at"),
            "title": item.get("title"),
            "request_kind": item.get("request_kind"),
        })
    summary.reverse()
    return {"counts": counts, "records": summary}


def read_json_body(handler: SimpleHTTPRequestHandler, max_bytes: int = 100 * 1024 * 1024) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length") or "0")
    if length <= 0:
        return {}
    if length > max_bytes:
        raise ValueError(f"JSON body too large: {length} bytes")
    data = json.loads(handler.rfile.read(length).decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("JSON body must be an object")
    return data


def decode_data_url(data_url: str) -> tuple[str, bytes]:
    match = re.match(r"^data:([^;,]+)?(;base64)?,(.*)$", data_url, re.DOTALL)
    if not match:
        raise ValueError("Invalid data_url")
    mime = match.group(1) or "application/octet-stream"
    payload = urllib.parse.unquote_to_bytes(match.group(3))
    if match.group(2):
        payload = base64.b64decode(payload)
    return mime, payload


def filename_from_media(field: str, item: dict[str, Any], mime: str = "image/png") -> str:
    raw = str(item.get("filename") or "").strip()
    if raw:
        return Path(raw).name
    if item.get("url"):
        path = urllib.parse.urlparse(str(item["url"])).path
        name = Path(urllib.parse.unquote(path)).name
        if name:
            return name
    suffix = mimetypes.guess_extension(mime) or ".png"
    return f"{field}{suffix}"


def media_item_to_file(field: str, item: Any) -> tuple[str, bytes] | None:
    if item in (None, "", False):
        return None
    if not isinstance(item, dict):
        raise ValueError(f"media.{field} must be an object")
    if item.get("data_url"):
        mime, blob = decode_data_url(str(item["data_url"]))
        return filename_from_media(field, item, mime), blob
    if item.get("url"):
        url = str(item["url"])
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                blob = resp.read()
                mime = resp.headers.get_content_type() or mimetypes.guess_type(url)[0] or "image/png"
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"参考素材下载失败 (HTTP {exc.code}): {url}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"参考素材下载失败 (连接错误): {url} — {exc}") from exc
        if not blob:
            raise ValueError(f"media.{field} url returned empty content")
        return filename_from_media(field, item, mime), blob
    raise ValueError(f"media.{field} must include data_url or url")


def job_id_response(job_id: str) -> dict[str, Any]:
    return {"ok": True, "job_id": job_id, "status_url": f"/api/jobs/{job_id}"}


def load_default_key() -> str:
    env_key = os.environ.get("NANO_BANANA_API_KEY") or os.environ.get("BANANA_API_KEY")
    if env_key:
        return env_key.strip()
    if DEFAULT_CONFIG.exists():
        try:
            data = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
            return str(data.get("api_key") or data.get("zhenzhen", {}).get("apikey") or "").strip()
        except Exception:
            return ""
    return ""


def mask_key(key: str) -> str:
    return f"{key[:5]}...{key[-4:]}" if key and len(key) > 12 else ("***" if key else "")


def request_json(method: str, url: str, api_key: str, body: dict[str, Any] | None = None, timeout: int = 600) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {api_key}"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {raw}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"API 请求超时或连接失败 ({exc.__class__.__name__}: {exc})") from exc


def request_gemini_generate(url: str, api_key: str, payload: dict[str, Any], timeout: int = 300) -> dict[str, Any]:
    return request_json("POST", url, api_key, payload, timeout=timeout)


def request_chat_completion(url: str, api_key: str, payload: dict[str, Any], timeout: int = 300) -> dict[str, Any]:
    return request_json("POST", url, api_key, payload, timeout=timeout)


def multipart_post(url: str, api_key: str, fields: dict[str, str], files: list[tuple[str, str, bytes, str]], timeout: int = 300) -> dict[str, Any]:
    boundary = f"----nanobanana{uuid.uuid4().hex}"
    body = bytearray()
    for name, value in fields.items():
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        body.extend(str(value).encode("utf-8"))
        body.extend(b"\r\n")
    for name, filename, blob, mime in files:
        body.extend(f"--{boundary}\r\n".encode())
        body.extend((f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                     f"Content-Type: {mime}\r\n\r\n").encode())
        body.extend(blob)
        body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        url,
        data=bytes(body),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"API 请求失败 (HTTP {exc.code}): {raw}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"API 请求超时或连接失败: {exc}") from exc


def get_field(form: cgi.FieldStorage | dict[str, Any], name: str, default: str = "") -> str:
    item = form[name] if name in form else None
    if item is None or getattr(item, "filename", None):
        return default
    return str(item.value)


def get_file(form: cgi.FieldStorage | dict[str, Any], name: str) -> tuple[str, bytes] | None:
    item = form[name] if name in form else None
    if item is None or not getattr(item, "filename", None):
        return None
    blob = item.file.read()
    return (Path(item.filename).name, blob) if blob else None


def read_preset(ws_id: str = "localhost") -> dict[str, Any]:
    path = _ws_preset_path(ws_id)
    if not path.exists():
        return {"values": {}, "media": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data.setdefault("values", {})
        data.setdefault("media", {})
        return data
    except Exception:
        pass
    return {"values": {}, "media": {}}


def preset_to_client(data: dict[str, Any], ws_id: str = "localhost") -> dict[str, Any]:
    media = {}
    media_dir = _ws_media_dir(ws_id)
    for field, item in data.get("media", {}).items():
        path = media_dir / item.get("stored", "")
        if path.exists():
            stored = path.name
            media[field] = {
                "filename": item.get("filename", path.name),
                "mime": item.get("mime", mimetypes.guess_type(path.name)[0] or "image/png"),
                "stored": stored,
                "url": f"/api/media/{urllib.parse.quote(stored)}?ws={ws_id}&v={int(path.stat().st_mtime)}",
            }
    return {"values": data.get("values", {}), "media": media}


def preset_for_client(ws_id: str = "localhost") -> dict[str, Any]:
    return preset_to_client(read_preset(ws_id), ws_id)


def copy_files_to_restore(values: dict[str, Any], files: dict[str, tuple[str, bytes]], prefix: str, ws_id: str = "localhost") -> dict[str, Any]:
    safe_values = {
        key: value for key, value in values.items()
        if key not in {"saved_media", "_auto_seed_base", "api_key", "api_key_override"}
    }
    media: dict[str, Any] = {}
    media_dir = _ws_media_dir(ws_id)
    media_dir.mkdir(parents=True, exist_ok=True)
    try:
        saved_media = json.loads(str(values.get("saved_media") or "{}"))
    except Exception:
        saved_media = {}
    if isinstance(saved_media, dict):
        for key, item in saved_media.items():
            if key not in FILE_FIELDS or not isinstance(item, dict):
                continue
            stored = Path(str(item.get("stored", ""))).name
            if stored and (media_dir / stored).exists():
                media[key] = {
                    "filename": item.get("filename", stored),
                    "stored": stored,
                    "mime": item.get("mime") or mimetypes.guess_type(stored)[0] or "image/png",
                }
    for key, file_data in files.items():
        if key not in FILE_FIELDS:
            continue
        filename, blob = file_data
        if not blob:
            continue
        suffix = Path(filename).suffix or mimetypes.guess_extension(mimetypes.guess_type(filename)[0] or "") or ".png"
        stored = f"{prefix}_{uuid.uuid4().hex}_{key}{suffix}"
        (media_dir / stored).write_bytes(blob)
        media[key] = {
            "filename": filename,
            "stored": stored,
            "mime": mimetypes.guess_type(filename)[0] or "image/png",
        }
    return {"values": safe_values, "media": media}


def activity_record_for_client(record: dict[str, Any] | None) -> dict[str, Any] | None:
    if not record:
        return None
    data = json.loads(json.dumps(record))
    if isinstance(data.get("restore"), dict):
        data["restore"] = preset_to_client(data["restore"])
        return data
    request = data.get("request") or {}
    values = ((request.get("parsed") or {}).get("values") or request.get("values") or {})
    if isinstance(values, dict) and values:
        media: dict[str, Any] = {}
        try:
            saved_media = json.loads(str(values.get("saved_media") or "{}"))
        except Exception:
            saved_media = {}
        if isinstance(saved_media, dict):
            for key, item in saved_media.items():
                if key not in FILE_FIELDS or not isinstance(item, dict):
                    continue
                stored = Path(str(item.get("stored", ""))).name
                if stored and (MEDIA_DIR / stored).exists():
                    media[key] = {
                        "filename": item.get("filename", stored),
                        "stored": stored,
                        "mime": item.get("mime") or mimetypes.guess_type(stored)[0] or "image/png",
                    }
        legacy = preset_to_client({"values": {
            key: value for key, value in values.items()
            if key not in {"api_key", "saved_media"} and key not in FILE_FIELDS
        }, "media": media})
        data["restore"] = {
            **legacy,
            "warning": "" if legacy.get("media") else "这条旧记录没有保存素材副本，只能恢复参数。",
        }
    return data


def parse_saved_media(form: cgi.FieldStorage | dict[str, Any]) -> dict[str, Any]:
    try:
        data = json.loads(get_field(form, "saved_media", "{}"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def get_file_or_saved(form: cgi.FieldStorage | dict[str, Any], name: str, ws_id: str = "localhost") -> tuple[str, bytes] | None:
    uploaded = get_file(form, name)
    if uploaded:
        return uploaded
    saved = parse_saved_media(form).get(name)
    if not isinstance(saved, dict):
        return None
    stored = Path(str(saved.get("stored", ""))).name
    if stored:
        path = _ws_media_dir(ws_id) / stored
        if path.exists():
            return (saved.get("filename", path.name), path.read_bytes())
    item = read_preset(ws_id).get("media", {}).get(name)
    if not item:
        return None
    path = _ws_media_dir(ws_id) / item.get("stored", "")
    return (item.get("filename", path.name), path.read_bytes()) if path.exists() else None


def collect_media_from_form(form: cgi.FieldStorage, ws_id: str = "localhost") -> dict[str, Any]:
    preset = read_preset(ws_id)
    active_media = preset.get("media", {})
    saved_media = parse_saved_media(form)
    media = {}
    media_dir = _ws_media_dir(ws_id)
    for key, item in saved_media.items():
        if not isinstance(item, dict):
            continue
        stored = Path(str(item.get("stored", ""))).name
        if stored and (media_dir / stored).exists():
            media[key] = {
                "filename": item.get("filename", stored),
                "stored": stored,
                "mime": item.get("mime") or mimetypes.guess_type(stored)[0] or "image/png",
            }
        elif key in active_media:
            media[key] = active_media[key]
    media_dir.mkdir(parents=True, exist_ok=True)
    for key in FILE_FIELDS:
        file_data = get_file(form, key)
        if not file_data:
            continue
        filename, blob = file_data
        suffix = Path(filename).suffix or ".png"
        stored = f"{uuid.uuid4().hex}_{key}{suffix}"
        (media_dir / stored).write_bytes(blob)
        media[key] = {"filename": filename, "stored": stored, "mime": mimetypes.guess_type(filename)[0] or "image/png"}
    # Preserve existing media for any fields not explicitly set
    for key in FILE_FIELDS:
        if key not in media and key in active_media:
            media[key] = active_media[key]
    return media


def collect_workspace_snapshot_from_form(form: cgi.FieldStorage, ws_id: str = "localhost") -> dict[str, Any]:
    values = {key: get_field(form, key) for key in VALUE_FIELDS if key in form and not getattr(form[key], "filename", None)}
    return {"values": values, "media": collect_media_from_form(form, ws_id)}


def collect_preset_from_form(form: cgi.FieldStorage, ws_id: str = "localhost") -> dict[str, Any]:
    return collect_workspace_snapshot_from_form(form, ws_id)


def write_active_preset(preset: dict[str, Any], ws_id: str) -> None:
    ws_dir = _ws_preset_path(ws_id).parent
    ws_dir.mkdir(parents=True, exist_ok=True)
    _ws_media_dir(ws_id).mkdir(parents=True, exist_ok=True)
    content = json.dumps(preset, ensure_ascii=False, indent=2)
    _atomic_write(_ws_preset_path(ws_id), content)


def safe_archive_name(raw: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_\-\u4e00-\u9fff]+", "_", (raw or "").strip()).strip("_")
    return name[:80] or time.strftime("nano_banana_%Y%m%d_%H%M%S")


def archive_path(name: str, ws_id: str = "localhost") -> Path:
    return STATE_DIR / "workspaces" / ws_id / "archives" / f"{safe_archive_name(name)}.nanobanana"


def list_archives(handler: SimpleHTTPRequestHandler | None = None) -> list[dict[str, Any]]:
    ws = _workspace_id(handler) if handler else "localhost"
    dir_path = STATE_DIR / "workspaces" / ws / "archives"
    dir_path.mkdir(parents=True, exist_ok=True)
    return [{"name": p.stem, "filename": p.name, "size": p.stat().st_size, "updated_at": int(p.stat().st_mtime)}
            for p in sorted(dir_path.glob("*.nanobanana"), key=lambda x: x.stat().st_mtime, reverse=True)]


def save_archive_file(name: str, preset: dict[str, Any], ws_id: str = "localhost", handler: SimpleHTTPRequestHandler | None = None) -> Path:
    path = archive_path(name, ws_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    safe_preset = dict(preset)
    safe_preset["values"] = {k: v for k, v in safe_preset.get("values", {}).items()
                             if k not in ("api_key", "api_key_override")}
    ws_media = _ws_media_dir(ws_id)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("preset.json", json.dumps(safe_preset, ensure_ascii=False, indent=2))
        for _, item in preset.get("media", {}).items():
            src = ws_media / item.get("stored", "")
            if src.exists():
                zf.write(src, f"media/{src.name}")
    return path


def load_archive_file(name: str, handler: SimpleHTTPRequestHandler | None = None) -> dict[str, Any]:
    ws = _workspace_id(handler) if handler else "localhost"
    path = archive_path(name, ws)
    migrated = False
    if not path.exists():
        # Legacy fallback: try top-level + IP directories
        legacy = ARCHIVE_DIR / f"{safe_archive_name(name)}.nanobanana"
        if legacy.exists():
            path = legacy
            migrated = True
        if not path.exists() and ARCHIVE_DIR.exists():
            for ip_dir in sorted(ARCHIVE_DIR.iterdir(), key=lambda d: d.stat().st_mtime, reverse=True):
                if ip_dir.is_dir():
                    p = ip_dir / f"{safe_archive_name(name)}.nanobanana"
                    if p.exists():
                        path = p
                        migrated = True
                        break
        if not path.exists():
            raise FileNotFoundError(f"Archive not found: {name}")
    ws_media = _ws_media_dir(ws)
    ws_media.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "r") as zf:
        preset = json.loads(zf.read("preset.json").decode("utf-8"))
        names = set(zf.namelist())
        for item in preset.get("media", {}).values():
            original = Path(str(item.get("stored", ""))).name
            archive_name = f"media/{original}"
            if archive_name not in names:
                continue
            target_name = f"{uuid.uuid4().hex}_{original}"
            (ws_media / target_name).write_bytes(zf.read(archive_name))
            item["stored"] = target_name
    write_active_preset(preset, ws)
    if migrated and handler is not None:
        save_archive_file(name, preset, ws)
    return preset_for_client(ws)


def choose_output_dir() -> str:
    prompt = "选择 Nano Banana 输出目录"
    if sys.platform == "darwin":
        result = subprocess.run(
            ["osascript", "-e", f'POSIX path of (choose folder with prompt "{prompt}")'],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return result.stdout.strip().rstrip("/")
    if sys.platform.startswith("win"):
        ps = (
            "$folder = (New-Object -ComObject Shell.Application)."
            f"BrowseForFolder(0, '{prompt}', 0, 0); "
            "if ($folder) { [Console]::OutputEncoding = [Text.UTF8Encoding]::UTF8; "
            "Write-Output $folder.Self.Path }"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
            check=True,
            capture_output=True,
            text=True,
        )
        selected = result.stdout.strip()
        if selected:
            return selected
        raise RuntimeError("未选择输出目录")

    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    try:
        selected = filedialog.askdirectory(title=prompt)
    finally:
        root.destroy()
    if selected:
        return selected
    raise RuntimeError("未选择输出目录")


def resolve_output_dir(raw: str | None) -> Path:
    path = Path(raw.strip()).expanduser() if raw and raw.strip() else OUTPUT_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def desktop_output_dir() -> str:
    desktop = Path.home() / "Desktop"
    parent = desktop if desktop.exists() else Path.home()
    return str((parent / "NanoBanana_outputs").resolve())


def open_output_dir(raw: str | None) -> str:
    path = resolve_output_dir(raw)
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    elif sys.platform.startswith("win"):
        os.startfile(str(path))  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", str(path)])
    return str(path)


def referenced_media_names() -> set[str]:
    names: set[str] = set()

    def collect_media(media: Any) -> None:
        if not isinstance(media, dict):
            return
        for item in media.values():
            if not isinstance(item, dict):
                continue
            stored = Path(str(item.get("stored", ""))).name
            if stored:
                names.add(stored)

    def collect_saved_media(values: Any) -> None:
        if not isinstance(values, dict):
            return
        try:
            saved = json.loads(str(values.get("saved_media") or "{}"))
        except Exception:
            saved = {}
        collect_media(saved)

    collect_media(read_preset().get("media"))
    for record in read_activity_log():
        collect_media((record.get("restore") or {}).get("media"))
        request = record.get("request") or {}
        collect_saved_media(request.get("values"))
        collect_saved_media((request.get("parsed") or {}).get("values"))
    return names


def cleanup_cache(media_days: int = 30, log_days: int = 14) -> dict[str, Any]:
    now = time.time()
    referenced = referenced_media_names()
    media_cutoff = now - max(1, media_days) * 86400
    log_cutoff = now - max(1, log_days) * 86400
    stats = {
        "ok": True,
        "media_days": media_days,
        "log_days": log_days,
        "media_deleted": 0,
        "logs_deleted": 0,
        "bytes_deleted": 0,
        "kept_referenced_media": len(referenced),
    }
    if MEDIA_DIR.exists():
        for path in MEDIA_DIR.iterdir():
            if not path.is_file() or path.name in referenced or path.stat().st_mtime >= media_cutoff:
                continue
            size = path.stat().st_size
            path.unlink()
            stats["media_deleted"] += 1
            stats["bytes_deleted"] += size
    logs_dir = ROOT / "logs"
    if logs_dir.exists():
        for path in logs_dir.iterdir():
            if not path.is_file() or path.stat().st_mtime >= log_cutoff:
                continue
            size = path.stat().st_size
            path.unlink()
            stats["logs_deleted"] += 1
            stats["bytes_deleted"] += size
    return stats


def api_schema() -> dict[str, Any]:
    config, config_error = load_provider_config()
    return {
        "app": "nano-banana",
        "endpoints": {
            "schema": "GET /api/schema",
            "submit_json": "POST /api/jobs/json",
            "job_status": "GET /api/jobs/{job_id}",
            "download": "GET /api/download/{token}",
        },
        "providers": config.get("providers", {}),
        "config_error": config_error,
        "value_fields": sorted(VALUE_FIELDS),
        "file_fields": sorted(FILE_FIELDS),
        "media_item": {
            "data_url": "data:image/png;base64,...",
            "url": "https://example.com/image.png",
            "filename": "optional-name.png",
        },
        "example": {
            "provider": "t8star",
            "model": "nano-banana-2",
            "mode": "img2img",
            "prompt": "图片提示词",
            "image_size": "2K",
            "repeat_count": 1,
            "concurrency": 1,
            "media": {
                "image_1": {"data_url": "data:image/png;base64,...", "filename": "image1.png"},
            },
        },
    }


def request_template() -> dict[str, Any]:
    config, config_error = load_provider_config()
    provider = str(config.get("default_provider") or "t8star")
    defaults = provider_defaults(config, provider)
    minimal = {
        "api_key": "YOUR_API_KEY",
        "prompt": "describe the image you want",
        "media": {
            "image_1": {
                "filename": "reference.png",
                "data_url": "data:image/png;base64,..."
            }
        },
    }
    full = {
        "api_key": "YOUR_API_KEY",
        "provider": provider,
        "base_url": defaults.get("base_url", DEFAULT_BASE_URL),
        "model": defaults.get("model", "nano-banana-2"),
        "custom_model": "",
        "mode": defaults.get("mode", "img2img"),
        "prompt": "describe the image you want",
        "aspect_ratio": defaults.get("aspect_ratio", "auto"),
        "image_size": defaults.get("image_size", "2K"),
        "response_format": defaults.get("response_format", "url"),
        "seed": "",
        "vary_seed": defaults.get("vary_seed", True),
        "repeat_count": defaults.get("repeat_count", 1),
        "concurrency": defaults.get("concurrency", 1),
        "poll_interval": defaults.get("poll_interval", 10),
        "timeout": defaults.get("timeout", 900),
        "resize_enabled": defaults.get("resize_enabled", False),
        "resize_width": defaults.get("resize_width", 1700),
        "resize_height": defaults.get("resize_height", 2500),
        "resize_interpolation": defaults.get("resize_interpolation", "high"),
        "resize_method": defaults.get("resize_method", "stretch"),
        "resize_condition": defaults.get("resize_condition", "always"),
        "resize_multiple_of": defaults.get("resize_multiple_of", 0),
        "media": {f"image_{i}": None for i in range(1, 15)},
    }
    full["media"]["image_1"] = {"filename": "reference.png", "data_url": "data:image/png;base64,..."}
    return {
        "ok": config_error is None,
        "app": "nano-banana",
        "endpoint": "POST /api/jobs/json",
        "content_type": "application/json",
        "config_error": config_error,
        "templates": {"minimal": minimal, "full": full},
        "field_notes": {
            "api_key": "可省略；省略时使用本地配置中的 key。",
            "custom_model": "高级字段；非空时覆盖 model 下拉值。",
            "media.image_1.data_url": "使用 data URL，例如 data:image/png;base64,...。",
            "repeat_count": "请求生成次数；如果 concurrency 更大，后端会按 concurrency 数启动。",
            "concurrency": "同一任务内同时运行的生成数量。",
            "seed": "为空且 vary_seed=true 时，后端会为每个 run 自动分配不复用 seed。",
        },
    }


def values_files_from_json(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, tuple[str, bytes]]]:
    config, config_error = load_provider_config()
    if config_error:
        raise ValueError(f"{config_error['message']}: {config_error['detail']}")
    incoming = {key: payload[key] for key in VALUE_FIELDS if key in payload and payload[key] is not None}
    provider = str(incoming.get("provider") or config.get("default_provider") or "t8star")
    values = provider_defaults(config, provider, str(incoming.get("model") or ""))
    values.update(incoming)
    values["provider"] = provider
    if values.get("custom_model"):
        values["model"] = str(values["custom_model"]).strip()
    values.setdefault("prompt", "")

    media = payload.get("media") or {}
    if not isinstance(media, dict):
        raise ValueError("media must be an object")
    files: dict[str, tuple[str, bytes]] = {}
    for field in FILE_FIELDS:
        if field not in media:
            continue
        file_data = media_item_to_file(field, media[field])
        if file_data:
            files[field] = file_data
    return values, files


def create_job(values: dict[str, Any], files: dict[str, tuple[str, bytes]], source: str, request_kind: str, request_data: dict[str, Any], ws_id: str = "localhost") -> str:
    job_id = uuid.uuid4().hex
    activity_id = uuid.uuid4().hex
    with LOCK:
        JOBS[job_id] = {"id": job_id, "status": "queued", "events": [], "results": [], "errors": [], "done": 0, "total": 0}
    response = job_id_response(job_id)
    record_activity({
        "id": activity_id,
        "job_id": job_id,
        "source": source,
        "request_kind": request_kind,
        "status": "running",
        "title": str(values.get("prompt") or "")[:80] or "Nano Banana task",
        "request": request_data,
        "response": response,
        "workspace_id": ws_id,
        "restore": copy_files_to_restore(values, files, activity_id, ws_id),
    })
    threading.Thread(target=run_job, args=(job_id, values, files, activity_id, ws_id), daemon=True).start()
    return job_id


def extract_items(result: dict[str, Any]) -> list[dict[str, Any]]:
    data = result.get("data")
    if isinstance(data, dict) and "data" in data:
        data = data.get("data")
    if isinstance(data, dict) and "status" in data and "data" in data:
        data = data.get("data")
    if isinstance(data, dict):
        data = data.get("data", data)
    if not isinstance(data, list):
        data = [data] if data else []
    return [x for x in data if isinstance(x, dict)]


def download_url(url: str, out_path: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            out_path.write_bytes(resp.read())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"生成结果下载失败 (HTTP {exc.code}): {url[:120]}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"生成结果下载失败 (连接错误): {url[:120]} — {exc}") from exc


def save_image_item(item: dict[str, Any], out_dir: Path, prefix: str, idx: int) -> tuple[str, str]:
    if item.get("url"):
        suffix = Path(urllib.parse.urlparse(item["url"]).path).suffix or ".png"
        out_path = out_dir / f"{prefix}_{idx}{suffix}"
        download_url(str(item["url"]), out_path)
        return str(item["url"]), str(out_path)
    if item.get("b64_json"):
        out_path = out_dir / f"{prefix}_{idx}.png"
        out_path.write_bytes(base64.b64decode(item["b64_json"]))
        return "", str(out_path)
    raise RuntimeError(f"No image data in result item: {item}")


def extract_gemini_images(result: dict[str, Any]) -> list[dict[str, str]]:
    images: list[dict[str, str]] = []
    candidates = result.get("candidates") if isinstance(result.get("candidates"), list) else []
    for candidate in candidates:
        content = candidate.get("content", {}) if isinstance(candidate, dict) else {}
        parts = content.get("parts", []) if isinstance(content, dict) else []
        for part in parts:
            if not isinstance(part, dict):
                continue
            image_node = part.get("inlineData") or part.get("inline_data")
            if isinstance(image_node, dict) and image_node.get("data"):
                images.append({
                    "b64_json": str(image_node["data"]),
                    "mime_type": str(image_node.get("mimeType") or image_node.get("mime_type") or "image/png"),
                })
    return images


def extract_chat_completion_images(result: dict[str, Any]) -> list[dict[str, str]]:
    images: list[dict[str, str]] = []
    for choice in result.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            for url in re.findall(r"https?://[^)\s]+", content):
                images.append({"url": url})
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                text = part.get("text")
                if isinstance(text, str):
                    for url in re.findall(r"https?://[^)\s]+", text):
                        images.append({"url": url})
                image_url = part.get("image_url")
                if isinstance(image_url, dict) and image_url.get("url"):
                    images.append({"url": str(image_url["url"])})
                if part.get("b64_json"):
                    images.append({"b64_json": str(part["b64_json"])})
        if message.get("b64_json"):
            images.append({"b64_json": str(message["b64_json"])})
    return images


def file_to_data_url(filename: str, blob: bytes) -> str:
    mime = mimetypes.guess_type(filename)[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(blob).decode('utf-8')}"


def save_gemini_image_item(item: dict[str, str], out_dir: Path, prefix: str, idx: int) -> tuple[str, str]:
    mime = item.get("mime_type", "image/png")
    suffix = mimetypes.guess_extension(mime) or ".png"
    if suffix == ".jpe":
        suffix = ".jpg"
    out_path = out_dir / f"{prefix}_{idx}{suffix}"
    data = item["b64_json"]
    missing_padding = len(data) % 4
    if missing_padding:
        data += "=" * (4 - missing_padding)
    out_path.write_bytes(base64.b64decode(data))
    return "", str(out_path)


def build_form(values: dict[str, Any], files: dict[str, tuple[str, bytes]]) -> dict[str, Any]:
    form: dict[str, Any] = {}
    for k, v in values.items():
        form[k] = type("Field", (), {"value": v, "filename": None})()
    for k, (filename, blob) in files.items():
        form[k] = type("Field", (), {"filename": filename, "file": type("Reader", (), {"read": lambda self, b=blob: b})()})()
    return form


def set_job(job_id: str, **updates: Any) -> None:
    with LOCK:
        JOBS[job_id].update(updates)


def add_event(job_id: str, message: str) -> None:
    with LOCK:
        JOBS[job_id].setdefault("events", []).append({"time": time.strftime("%H:%M:%S"), "message": message})


def truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"on", "true", "1", "yes"}


def run_one(job_id: str, index: int, values: dict[str, Any], files: dict[str, tuple[str, bytes]], ws_id: str = "localhost") -> dict[str, Any]:
    form = build_form(values, files)
    api_key = str(values["api_key"]).strip()
    base_url = str(values.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
    provider = get_field(form, "provider", "t8star")
    mode = get_field(form, "mode", "img2img")
    seed_raw = get_field(form, "seed", "").strip()
    auto_seed_base = int(values.get("_auto_seed_base") or 0)
    seed = int(seed_raw) if seed_raw else auto_seed_base
    if seed > 0 and truthy(get_field(form, "vary_seed")):
        seed += index - 1
        seed = ((seed - 1) % MAX_SEED) + 1

    common = {
        "prompt": get_field(form, "prompt").strip(),
        "model": get_field(form, "custom_model").strip() or get_field(form, "model", "nano-banana-2"),
        "aspect_ratio": get_field(form, "aspect_ratio", "auto"),
        "response_format": get_field(form, "response_format", "url"),
    }
    image_size = get_field(form, "image_size", "2K")
    if image_size:
        common["image_size"] = image_size
    if seed > 0:
        common["seed"] = str(seed)

    seed_label = f", seed:{seed}" if seed > 0 else ""
    add_event(job_id, f"Run {index}: submitting {provider}/{mode}{seed_label}")
    if provider == "gemini":
        image_count = 0
        if common["model"] == "gpt-image-2":
            content: list[dict[str, Any]] = [{"type": "text", "text": common["prompt"]}]
            if mode != "text2img":
                for i in range(1, 15):
                    file_data = get_file_or_saved(form, f"image_{i}", ws_id)
                    if not file_data:
                        continue
                    filename, blob = file_data
                    content.append({"type": "image_url", "image_url": {"url": file_to_data_url(filename, blob)}})
                    image_count += 1
            payload = {
                "model": common["model"],
                "messages": [{"role": "user", "content": content}],
                "max_tokens": 256,
            }
            result = request_chat_completion(f"{base_url}/v1/chat/completions", api_key, payload)
            task_id = f"chat_{uuid.uuid4().hex[:12]}"
            items = extract_chat_completion_images(result)
            if not items:
                raise RuntimeError(f"No image result found: {result}")
            out_dir = resolve_output_dir(values.get("output_dir"))
            file_token_results = []
            custom_name = values.get("output_name", "").strip()
            if custom_name:
                total = max(1, int(values.get("repeat_count") or 1), int(values.get("concurrency") or 1))
                prefix = f"{custom_name}-{index}" if total > 1 else custom_name
            else:
                prefix = f"{time.strftime('%Y%m%d_%H%M%S')}_run{index}_{task_id}"
            for i, item in enumerate(items, 1):
                image_url, local_path = save_image_item(item, out_dir, prefix, i)
                token = uuid.uuid4().hex
                with LOCK:
                    FILES[token] = Path(local_path)
                file_token_results.append({
                    "image_url": image_url,
                    "download_url": f"/api/download/{token}",
                    "filename": Path(local_path).name,
                    "local_path": local_path,
                })
            add_event(job_id, f"Run {index}: saved {len(file_token_results)} image(s), input_images:{image_count}")
            return {"index": index, "task_id": task_id, "status": "succeeded", "seed": seed or None, "images": file_token_results}

        parts: list[dict[str, Any]] = [{"text": common["prompt"]}]
        if mode != "text2img":
            for i in range(1, 15):
                file_data = get_file_or_saved(form, f"image_{i}", ws_id)
                if not file_data:
                    continue
                filename, blob = file_data
                parts.append({
                    "inline_data": {
                        "mime_type": mimetypes.guess_type(filename)[0] or "image/png",
                        "data": base64.b64encode(blob).decode("utf-8"),
                    }
                })
                image_count += 1
        generation_config: dict[str, Any] = {"imageConfig": {}}
        if common.get("aspect_ratio") and common["aspect_ratio"] != "auto":
            generation_config["imageConfig"]["aspectRatio"] = common["aspect_ratio"]
        if image_size:
            generation_config["imageConfig"]["imageSize"] = image_size
        if seed > 0:
            generation_config["seed"] = seed
        model_path = urllib.parse.quote(common["model"], safe="")
        result = request_gemini_generate(
            f"{base_url}/v1beta/models/{model_path}:generateContent",
            api_key,
            {"contents": [{"parts": parts}], "generationConfig": generation_config},
        )
        task_id = f"gemini_{uuid.uuid4().hex[:12]}"
        items = extract_gemini_images(result)
        if not items:
            raise RuntimeError(f"No image result found: {result}")
        out_dir = resolve_output_dir(values.get("output_dir"))
        file_token_results = []
        custom_name = values.get("output_name", "").strip()
        if custom_name:
            total = max(1, int(values.get("repeat_count") or 1), int(values.get("concurrency") or 1))
            prefix = f"{custom_name}-{index}" if total > 1 else custom_name
        else:
            prefix = f"{time.strftime('%Y%m%d_%H%M%S')}_run{index}_{task_id}"
        for i, item in enumerate(items, 1):
            image_url, local_path = save_gemini_image_item(item, out_dir, prefix, i)
            token = uuid.uuid4().hex
            with LOCK:
                FILES[token] = Path(local_path)
            file_token_results.append({
                "image_url": image_url,
                "download_url": f"/api/download/{token}",
                "filename": Path(local_path).name,
                "local_path": local_path,
            })
        add_event(job_id, f"Run {index}: saved {len(file_token_results)} image(s), input_images:{image_count}")
        return {"index": index, "task_id": task_id, "status": "succeeded", "seed": seed or None, "images": file_token_results}

    if mode == "text2img":
        payload = dict(common)
        if seed > 0:
            payload["seed"] = seed
        result = request_json("POST", f"{base_url}/v1/images/generations?async=true", api_key, payload)
        image_count = 0
    else:
        files_payload = []
        image_count = 0
        for i in range(1, 15):
            file_data = get_file_or_saved(form, f"image_{i}", ws_id)
            if not file_data:
                continue
            filename, blob = file_data
            mime = mimetypes.guess_type(filename)[0] or "image/png"
            files_payload.append(("image", filename, blob, mime))
            image_count += 1
        result = multipart_post(f"{base_url}/v1/images/edits?async=true", api_key, common, files_payload)

    task_id = result.get("task_id") or result.get("id") or f"sync_{uuid.uuid4().hex[:12]}"
    add_event(job_id, f"Run {index}: task {task_id}, input_images:{image_count}")
    if not result.get("task_id") and result.get("data"):
        final = result
    else:
        status_url = f"{base_url}/v1/images/tasks/{task_id}"
        timeout = int(values.get("timeout") or 900)
        interval = int(values.get("poll_interval") or 10)
        start = time.time()
        final = {}
        while True:
            if time.time() - start > timeout:
                raise RuntimeError(f"Task {task_id} timed out after {timeout}s")
            time.sleep(interval)
            status = request_json("GET", status_url, api_key, timeout=60)
            data = status.get("data") if isinstance(status.get("data"), dict) else status
            state = str(data.get("status", "")).lower() if isinstance(data, dict) else ""
            add_event(job_id, f"Run {index}: {state or 'unknown'}")
            if state in {"success", "succeeded", "completed", "done", "finished"} or (isinstance(data, dict) and data.get("data")):
                final = data
                break
            if state in {"failed", "failure", "error", "cancelled", "canceled"}:
                raise RuntimeError(f"Task {task_id} ended as {state}: {status}")

    items = extract_items(final)
    if not items:
        raise RuntimeError(f"No image result found: {final}")
    out_dir = resolve_output_dir(values.get("output_dir"))
    file_token_results = []
    custom_name = values.get("output_name", "").strip()
    if custom_name:
        total = max(1, int(values.get("repeat_count") or 1), int(values.get("concurrency") or 1))
        prefix = f"{custom_name}-{index}" if total > 1 else custom_name
    else:
        prefix = f"{time.strftime('%Y%m%d_%H%M%S')}_run{index}_{task_id}"
    for i, item in enumerate(items, 1):
        image_url, local_path = save_image_item(item, out_dir, prefix, i)
        token = uuid.uuid4().hex
        with LOCK:
            FILES[token] = Path(local_path)
        file_token_results.append({
            "image_url": image_url,
            "download_url": f"/api/download/{token}",
            "filename": Path(local_path).name,
            "local_path": local_path,
        })
    add_event(job_id, f"Run {index}: saved {len(file_token_results)} image(s)")
    return {"index": index, "task_id": task_id, "status": "succeeded", "seed": seed or None, "images": file_token_results}


def run_job(job_id: str, values: dict[str, Any], files: dict[str, tuple[str, bytes]], activity_id: str | None = None, ws_id: str = "localhost") -> None:
    try:
        requested_count = max(1, min(50, int(values.get("repeat_count") or 1)))
        requested_concurrency = max(1, min(20, int(values.get("concurrency") or 1)))
        count = max(requested_count, requested_concurrency)
        concurrency = min(count, requested_concurrency)
        if not str(values.get("seed") or "").strip() and truthy(values.get("vary_seed")):
            values = dict(values)
            values["_auto_seed_base"] = secrets.randbelow(MAX_SEED) + 1
        set_job(job_id, status="running", total=count, done=0, results=[], errors=[])
        add_event(job_id, f"Started {count} run(s), concurrency {concurrency}, key {mask_key(values.get('api_key', ''))}")
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [pool.submit(run_one, job_id, i, values, files, ws_id) for i in range(1, count + 1)]
            for future in concurrent.futures.as_completed(futures):
                try:
                    result = future.result()
                    with LOCK:
                        JOBS[job_id]["results"].append(result)
                        JOBS[job_id]["done"] += 1
                except Exception as exc:
                    with LOCK:
                        JOBS[job_id]["errors"].append(str(exc))
                        JOBS[job_id]["done"] += 1
                    add_event(job_id, f"Error: {exc}")
        with LOCK:
            errors = JOBS[job_id]["errors"]
            final_job = json.loads(json.dumps(JOBS[job_id]))
        final_status = "failed" if errors else "succeeded"
        set_job(job_id, status=final_status)
        final_job["status"] = final_status
        update_activity(activity_id, status=final_status, result=final_job)
        add_event(job_id, "Finished")
    except Exception as exc:
        set_job(job_id, status="failed", errors=[str(exc)])
        with LOCK:
            final_job = json.loads(json.dumps(JOBS.get(job_id, {})))
        update_activity(activity_id, status="failed", error=str(exc), result=final_job)
        add_event(job_id, f"Fatal: {exc}")


class Handler(SimpleHTTPRequestHandler):
    def translate_path(self, path: str) -> str:
        path = urllib.parse.urlparse(path).path
        if path in {"/", "/index.html"}:
            return str(STATIC_DIR / "index.html")
        return str((STATIC_DIR / path.lstrip("/")).resolve())

    def do_GET(self) -> None:
        if self.path == "/api/v1/meta":
            json_response(self, 200, {
                "app": "nano-banana",
                "version": "1.0.0",
                "port": int(os.environ.get("PORT", "8797")),
                "capabilities": ["text2img", "img2img"],
                "status": "ready",
            })
            return
        if self.path == "/api/config":
            providers, config_error = load_provider_config()
            json_response(self, 200, {
                "ok": config_error is None,
                "has_key": bool(load_default_key()),
                "masked_key": mask_key(load_default_key()),
                "providers": providers.get("providers", {}),
                "default_provider": providers.get("default_provider"),
                "config_error": config_error,
            })
            return
        if self.path == "/api/request-template":
            json_response(self, 200, request_template())
            return
        if self.path == "/api/preset":
            json_response(self, 200, preset_for_client(_workspace_id(self)))
            return
        if self.path == "/api/archives":
            json_response(self, 200, {"archives": list_archives(self)})
            return
        if self.path == "/api/schema":
            json_response(self, 200, api_schema())
            return
        if self.path == "/api/activity":
            json_response(self, 200, activity_list())
            return
        if self.path.startswith("/api/activity/"):
            activity_id = self.path.rsplit("/", 1)[-1]
            record = next((item for item in read_activity_log() if item.get("id") == activity_id), None)
            json_response(self, 200 if record else 404, activity_record_for_client(record) or {"error": "activity not found"})
            return
        if self.path == "/api/default-output-dir":
            json_response(self, 200, {"path": desktop_output_dir()})
            return
        if self.path.startswith("/api/preset-media/"):
            field = self.path.rsplit("/", 1)[-1]
            ws = _workspace_id(self)
            item = read_preset(ws).get("media", {}).get(field)
            path = _ws_media_dir(ws) / item.get("stored", "") if item else None
            if not item or not path or not path.exists():
                json_response(self, 404, {"error": "media not found"})
                return
            mime = item.get("mime") or "image/png"
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(path.stat().st_size))
            self.end_headers()
            try:
                self.wfile.write(path.read_bytes())
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            return
        if urllib.parse.urlparse(self.path).path.startswith("/api/media/"):
            raw_name = urllib.parse.urlparse(self.path).path.rsplit("/", 1)[-1]
            stored = Path(urllib.parse.unquote(raw_name)).name
            ws = _workspace_id(self)
            path = _ws_media_dir(ws) / stored
            if not path.exists():
                json_response(self, 404, {"error": "media not found"})
                return
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "image/png")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(path.stat().st_size))
            self.end_headers()
            try:
                self.wfile.write(path.read_bytes())
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            return
        if self.path.startswith("/api/jobs/"):
            job_id = self.path.rsplit("/", 1)[-1]
            with LOCK:
                job = JOBS.get(job_id)
                data = json.loads(json.dumps(job)) if job else None
            json_response(self, 200 if data else 404, data or {"error": "job not found"})
            return
        if self.path.startswith("/api/download/"):
            token = self.path.rsplit("/", 1)[-1]
            with LOCK:
                path = FILES.get(token)
            if not path or not path.exists():
                json_response(self, 404, {"error": "file not found"})
                return
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
            self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
            self.send_header("Content-Length", str(path.stat().st_size))
            self.end_headers()
            try:
                self.wfile.write(path.read_bytes())
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            return
        super().do_GET()

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        if os.environ.get("CORS") == "1":
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Max-Age", "86400")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:
        if self.path == "/api/choose-output-dir":
            client_ip = self.headers.get("X-Forwarded-For") or self.client_address[0]
            if client_ip not in ("127.0.0.1", "::1", "localhost"):
                json_response(self, 200, {"remote": True})
                return
            try:
                json_response(self, 200, {"path": choose_output_dir()})
            except Exception as exc:
                json_response(self, 500, {"error": str(exc)})
            return
        if self.path == "/api/open-output-dir":
            client_ip = self.headers.get("X-Forwarded-For") or self.client_address[0]
            if client_ip not in ("127.0.0.1", "::1", "localhost"):
                json_response(self, 200, {"remote": True})
                return
            form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={"REQUEST_METHOD": "POST"})
            try:
                json_response(self, 200, {"ok": True, "path": open_output_dir(get_field(form, "output_dir"))})
            except Exception as exc:
                json_response(self, 500, api_error("open_output_dir_failed", "打开输出目录失败", str(exc)))
            return
        if self.path == "/api/cleanup-cache":
            client_ip = self.headers.get("X-Forwarded-For") or self.client_address[0]
            if client_ip not in ("127.0.0.1", "::1", "localhost"):
                json_response(self, 200, {"remote": True})
                return
            try:
                json_response(self, 200, cleanup_cache())
            except Exception as exc:
                json_response(self, 500, api_error("cleanup_cache_failed", "清理缓存失败", str(exc)))
            return
        if self.path == "/api/workspace/snapshot":
            form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={"REQUEST_METHOD": "POST"})
            try:
                ws = _workspace_id(self)
                json_response(self, 200, preset_to_client(collect_workspace_snapshot_from_form(form, ws), ws))
            except Exception as exc:
                json_response(self, 500, {"error": str(exc)})
            return
        if self.path == "/api/preset":
            form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={"REQUEST_METHOD": "POST"})
            ws = _workspace_id(self)
            preset = collect_preset_from_form(form, ws)
            write_active_preset(preset, ws)
            archive_name = get_field(form, "archive_name")
            archive = save_archive_file(archive_name, preset, ws).name if archive_name.strip() else None
            data = preset_for_client(ws)
            data["archive"] = archive
            data["archives"] = list_archives(self)
            json_response(self, 200, data)
            return
        if self.path == "/api/archive/load":
            form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={"REQUEST_METHOD": "POST"})
            try:
                data = load_archive_file(get_field(form, "archive_name"), self)
                data["archives"] = list_archives(self)
                json_response(self, 200, data)
            except Exception as exc:
                json_response(self, 400, {"error": str(exc)})
            return
        if self.path == "/api/archive/delete":
            if not _is_local(self):
                json_response(self, 403, {"ok": False, "error": "admin only"})
                return
            form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={"REQUEST_METHOD": "POST"})
            path = archive_path(get_field(form, "archive_name"), _workspace_id(self))
            if path.exists():
                path.unlink()
            json_response(self, 200, {"archives": list_archives(self)})
            return
        if self.path == "/api/preset/clear":
            if not _is_local(self):
                json_response(self, 403, {"ok": False, "error": "admin only"})
                return
            ws = _workspace_id(self)
            ws_dir = STATE_DIR / "workspaces" / ws
            if ws_dir.exists():
                shutil.rmtree(ws_dir)
            json_response(self, 200, {"ok": True})
            return
        if self.path == "/api/jobs/json":
            try:
                payload = read_json_body(self)
                values, files = values_files_from_json(payload)
                api_key = str(values.get("api_key") or load_default_key()).strip()
                if not api_key and not payload.get("dry_run"):
                    json_response(self, 400, api_error("invalid_request", "API key is required"))
                    return
                if api_key:
                    values["api_key"] = api_key
                if payload.get("dry_run"):
                    response = {
                        "ok": True,
                        "dry_run": True,
                        "values": {k: ("***" if k == "api_key" else v) for k, v in values.items()},
                        "files": {k: {"filename": v[0], "bytes": len(v[1])} for k, v in files.items()},
                    }
                    record_activity({
                        "source": "api",
                        "request_kind": "json_dry_run",
                        "status": "succeeded",
                        "title": str(values.get("prompt") or "")[:80] or "Nano Banana dry run",
                        "request": summarize_payload(payload),
                        "response": response,
                    })
                    json_response(self, 200, response)
                    return
                request_data = {"raw": summarize_payload(payload), "parsed": summarize_values_files(values, files)}
                ws = _workspace_id(self)
                job_id = create_job(values, files, "api", "json", request_data, ws)
                json_response(self, 200, job_id_response(job_id))
            except Exception as exc:
                json_response(self, 400, api_error("invalid_request", str(exc)))
            return
        if self.path != "/api/jobs":
            json_response(self, 404, {"error": "not found"})
            return
        form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={"REQUEST_METHOD": "POST"})
        api_key = get_field(form, "api_key") or load_default_key()
        if not api_key:
            json_response(self, 400, api_error("invalid_request", "API key is required"))
            return
        values = {key: get_field(form, key) for key in form.keys() if not getattr(form[key], "filename", None)}
        values["api_key"] = api_key
        files = {}
        for key in form.keys():
            item = form[key]
            if getattr(item, "filename", None):
                blob = item.file.read()
                if blob:
                    files[key] = (Path(item.filename).name, blob)
        request_data = summarize_values_files(values, files)
        ws = _workspace_id(self)
        job_id = create_job(values, files, "page", "multipart", request_data, ws)
        json_response(self, 200, job_id_response(job_id))


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    port = int(os.environ.get("PORT", "8797"))
    host = os.environ.get("HOST", "127.0.0.1")
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Nano Banana GUI running at http://{host}:{port}")
    print("Press Ctrl+C to stop")

    def shutdown_handler(*args):
        print("\nShutting down...")
        server.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, shutdown_handler)
    elif hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, shutdown_handler)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
