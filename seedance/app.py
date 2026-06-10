#!/usr/bin/env python3
from __future__ import annotations

import base64
import cgi
import concurrent.futures
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.error
import urllib.request
import uuid
import zipfile
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
OUTPUT_DIR = ROOT / "outputs"
STATE_DIR = ROOT / "state"
MEDIA_DIR = STATE_DIR / "media"
PRESET_PATH = STATE_DIR / "preset.json"
ACTIVITY_PATH = STATE_DIR / "activity_log.json"
ARCHIVE_DIR = ROOT / "archives"
PROVIDERS_PATH = ROOT / "providers.json"
DEFAULT_BASE_URL = "https://ai.t8star.cn"
OFFICIAL_ARK_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
DEFAULT_CONFIG = Path.home() / "ComfyUI/custom_nodes/Comfyui-zhenzhen/Comflyapi.json"
TERMINAL_STATUSES = {"succeeded", "success", "failed", "fail", "failure", "cancelled", "canceled"}

JOBS: dict[str, dict[str, Any]] = {}
FILES: dict[str, Path] = {}
JOBS_LOCK = threading.Lock()
ACTIVITY_LIMIT = 300

FILE_FIELDS = {
    "first_frame",
    "last_frame",
    *{f"ref_image_{i}" for i in range(1, 10)},
    *{f"ref_video_{i}" for i in range(1, 4)},
    *{f"ref_audio_{i}" for i in range(1, 4)},
}
VALUE_FIELDS = {
    "api_key",
    "provider",
    "base_url",
    "output_dir",
    "model",
    "custom_model",
    "duration",
    "resolution",
    "ratio",
    "seed",
    "generate_audio",
    "watermark",
    "return_last_frame",
    "web_search",
    "repeat_count",
    "concurrency",
    "poll_interval",
    "timeout",
    "vary_seed",
    "output_name",
    "prompt",
}

FALLBACK_PROVIDERS = {
    "schema_version": 1,
    "app": "seedance",
    "default_provider": "t8star",
    "providers": {
        "t8star": {
            "label": "T8Star 兼容接口",
            "base_url": DEFAULT_BASE_URL,
            "api_style": "t8star_seedance",
            "hint": "使用原有 T8Star 兼容接口，素材会先上传到 /v1/files。",
            "defaults": {"model": "doubao-seedance-2-0-260128", "duration": 12, "resolution": "720p", "ratio": "16:9", "repeat_count": 1, "concurrency": 1, "poll_interval": 10, "timeout": 3600, "vary_seed": True},
            "models": [{"id": "doubao-seedance-2-0-260128", "label": "doubao-seedance-2-0-260128"}, {"id": "doubao-seedance-2-0-fast-260128", "label": "doubao-seedance-2-0-fast-260128"}],
        },
        "volcengine": {
            "label": "豆包官方 / 火山方舟",
            "base_url": OFFICIAL_ARK_BASE_URL,
            "api_style": "ark_seedance",
            "hint": "使用豆包官方火山方舟 API。首尾帧模式不能与参考素材混用；本地素材会以 data URL 发送。",
            "defaults": {"model": "doubao-seedance-2-0-260128", "duration": 12, "resolution": "720p", "ratio": "16:9", "repeat_count": 1, "concurrency": 1, "poll_interval": 10, "timeout": 3600, "vary_seed": True},
            "models": [{"id": "doubao-seedance-2-0-260128", "label": "doubao-seedance-2-0-260128"}, {"id": "doubao-seedance-2-0-fast-260128", "label": "doubao-seedance-2-0-fast-260128"}],
        },
    },
}


def load_default_key() -> str:
    env_key = os.environ.get("SEEDANCE_API_KEY") or os.environ.get("ZHENZHEN_API_KEY")
    if env_key:
        return env_key.strip()
    if DEFAULT_CONFIG.exists():
        try:
            data = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
            return str(data.get("api_key") or data.get("zhenzhen", {}).get("apikey") or "").strip()
        except Exception:
            return ""
    return ""


def json_response(handler: SimpleHTTPRequestHandler, status: int, data: dict[str, Any]) -> None:
    raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    if os.environ.get("CORS") == "1":
        handler.send_header("Access-Control-Allow-Origin", "*")
        handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        handler.send_header("Access-Control-Allow-Headers", "Content-Type")
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
        if config.get("schema_version") != 1 or config.get("app") != "seedance":
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
    ACTIVITY_PATH.write_text(json.dumps(items[-ACTIVITY_LIMIT:], ensure_ascii=False, indent=2), encoding="utf-8")


def summarize_payload(value: Any) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key == "api_key":
                result[key] = mask_key(str(item))
            elif key == "media" and isinstance(item, dict):
                result[key] = {
                    name: summarize_media_item(media_item)
                    for name, media_item in item.items()
                }
            else:
                result[key] = summarize_payload(item)
        return result
    if isinstance(value, list):
        return [summarize_payload(item) for item in value]
    if isinstance(value, str) and value.startswith("data:"):
        return {"data_url": True, "chars": len(value)}
    return value


def summarize_media_item(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    result = {key: value for key, value in item.items() if key not in {"data_url"}}
    if item.get("data_url"):
        result["data_url"] = True
        result["chars"] = len(str(item["data_url"]))
    return result


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


def read_json_body(handler: SimpleHTTPRequestHandler, max_bytes: int = 200 * 1024 * 1024) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length") or "0")
    if length <= 0:
        return {}
    if length > max_bytes:
        raise ValueError(f"JSON body too large: {length} bytes")
    raw = handler.rfile.read(length).decode("utf-8")
    data = json.loads(raw)
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


def filename_from_media(field: str, item: dict[str, Any], mime: str = "application/octet-stream") -> str:
    raw = str(item.get("filename") or "").strip()
    if raw:
        return Path(raw).name
    if item.get("url"):
        path = urllib.parse.urlparse(str(item["url"])).path
        name = Path(urllib.parse.unquote(path)).name
        if name:
            return name
    suffix = mimetypes.guess_extension(mime) or ".bin"
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
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req, timeout=180) as resp:
            blob = resp.read()
            mime = resp.headers.get_content_type() or mimetypes.guess_type(url)[0] or "application/octet-stream"
        if not blob:
            raise ValueError(f"media.{field} url returned empty content")
        return filename_from_media(field, item, mime), blob
    raise ValueError(f"media.{field} must include data_url or url")


def job_id_response(job_id: str) -> dict[str, Any]:
    return {"ok": True, "job_id": job_id, "status_url": f"/api/jobs/{job_id}"}


def read_preset() -> dict[str, Any]:
    if not PRESET_PATH.exists():
        return {"values": {}, "media": {}}
    try:
        data = json.loads(PRESET_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("values", {})
            data.setdefault("media", {})
            return data
    except Exception:
        pass
    return {"values": {}, "media": {}}


def preset_to_client(data: dict[str, Any]) -> dict[str, Any]:
    media = {}
    for field, item in data.get("media", {}).items():
        path = MEDIA_DIR / item.get("stored", "")
        if path.exists():
            stored = path.name
            media[field] = {
                "filename": item.get("filename", path.name),
                "mime": item.get("mime", mimetypes.guess_type(path.name)[0] or "application/octet-stream"),
                "stored": stored,
                "url": f"/api/media/{urllib.parse.quote(stored)}?v={int(path.stat().st_mtime)}",
            }
    return {"values": data.get("values", {}), "media": media}


def preset_for_client() -> dict[str, Any]:
    return preset_to_client(read_preset())


def copy_files_to_restore(values: dict[str, Any], files: dict[str, tuple[str, bytes]], prefix: str) -> dict[str, Any]:
    safe_values = {
        key: value for key, value in values.items()
        if key not in {"api_key", "saved_media"}
    }
    media: dict[str, Any] = {}
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
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
                    "mime": item.get("mime") or mimetypes.guess_type(stored)[0] or "application/octet-stream",
                }
    for key, file_data in files.items():
        if key not in FILE_FIELDS:
            continue
        filename, blob = file_data
        if not blob:
            continue
        suffix = Path(filename).suffix or mimetypes.guess_extension(mimetypes.guess_type(filename)[0] or "") or ".bin"
        stored = f"{prefix}_{uuid.uuid4().hex}_{key}{suffix}"
        (MEDIA_DIR / stored).write_bytes(blob)
        media[key] = {
            "filename": filename,
            "stored": stored,
            "mime": mimetypes.guess_type(filename)[0] or "application/octet-stream",
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
                        "mime": item.get("mime") or mimetypes.guess_type(stored)[0] or "application/octet-stream",
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


def safe_archive_name(raw: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_\-\u4e00-\u9fff]+", "_", (raw or "").strip()).strip("_")
    return name[:80] or time.strftime("seedance_%Y%m%d_%H%M%S")


def archive_path(name: str) -> Path:
    return ARCHIVE_DIR / f"{safe_archive_name(name)}.seedance"


def list_archives() -> list[dict[str, Any]]:
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    items = []
    for path in sorted(ARCHIVE_DIR.glob("*.seedance"), key=lambda p: p.stat().st_mtime, reverse=True):
        items.append(
            {
                "name": path.stem,
                "filename": path.name,
                "size": path.stat().st_size,
                "updated_at": int(path.stat().st_mtime),
            }
        )
    return items


def collect_media_from_form(form: cgi.FieldStorage) -> dict[str, Any]:
    preset = read_preset()
    active_media = preset.get("media", {})
    saved_media = parse_saved_media(form)
    media = {}
    for key, item in saved_media.items():
        if not isinstance(item, dict):
            continue
        stored = Path(str(item.get("stored", ""))).name
        if stored and (MEDIA_DIR / stored).exists():
            media[key] = {
                "filename": item.get("filename", stored),
                "stored": stored,
                "mime": item.get("mime") or mimetypes.guess_type(stored)[0] or "application/octet-stream",
            }
        elif key in active_media:
            media[key] = active_media[key]
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    for key in FILE_FIELDS:
        file_data = get_file(form, key)
        if not file_data:
            continue
        filename, blob = file_data
        suffix = Path(filename).suffix or mimetypes.guess_extension(mimetypes.guess_type(filename)[0] or "") or ".bin"
        stored = f"{uuid.uuid4().hex}_{key}{suffix}"
        path = MEDIA_DIR / stored
        path.write_bytes(blob)
        media[key] = {
            "filename": filename,
            "stored": stored,
            "mime": mimetypes.guess_type(filename)[0] or "application/octet-stream",
        }
    return media


def collect_workspace_snapshot_from_form(form: cgi.FieldStorage) -> dict[str, Any]:
    values = {key: get_field(form, key) for key in VALUE_FIELDS if key in form and not getattr(form[key], "filename", None)}
    return {"values": values, "media": collect_media_from_form(form)}


def collect_preset_from_form(form: cgi.FieldStorage) -> dict[str, Any]:
    return collect_workspace_snapshot_from_form(form)


def write_active_preset(preset: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    PRESET_PATH.write_text(json.dumps(preset, ensure_ascii=False, indent=2), encoding="utf-8")


def save_archive_file(name: str, preset: dict[str, Any]) -> Path:
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    path = archive_path(name)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("preset.json", json.dumps(preset, ensure_ascii=False, indent=2))
        for field, item in preset.get("media", {}).items():
            stored = item.get("stored", "")
            src = MEDIA_DIR / stored
            if src.exists():
                zf.write(src, f"media/{stored}")
    return path


def load_archive_file(name: str) -> dict[str, Any]:
    path = archive_path(name)
    if not path.exists():
        raise FileNotFoundError(f"Archive not found: {name}")
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "r") as zf:
        preset = json.loads(zf.read("preset.json").decode("utf-8"))
        names = set(zf.namelist())
        for item in preset.get("media", {}).values():
            original = Path(str(item.get("stored", ""))).name
            archive_name = f"media/{original}"
            if archive_name not in names:
                continue
            target_name = f"{uuid.uuid4().hex}_{original}"
            target = MEDIA_DIR / target_name
            target.write_bytes(zf.read(archive_name))
            item["stored"] = target_name
    write_active_preset(preset)
    return preset_for_client()


def choose_output_dir() -> str:
    prompt = "选择 Seedance 输出目录"
    if sys.platform == "darwin":
        script = f'POSIX path of (choose folder with prompt "{prompt}")'
        result = subprocess.run(["osascript", "-e", script], check=True, capture_output=True, text=True)
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


def mask_key(key: str) -> str:
    if not key:
        return ""
    return f"{key[:5]}...{key[-4:]}" if len(key) > 12 else "***"


def normalize_status(value: str | None) -> str:
    status = (value or "").lower()
    if status == "success":
        return "succeeded"
    if status in {"fail", "failure"}:
        return "failed"
    return status


def request_json(method: str, url: str, api_key: str, body: dict[str, Any] | None = None, timeout: int = 180) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {api_key}"}
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {raw}") from exc


def upload_file(base_url: str, api_key: str, blob: bytes, filename: str, content_type: str) -> str:
    boundary = f"----seedance{uuid.uuid4().hex}"
    body = bytearray()
    body.extend(f"--{boundary}\r\n".encode())
    body.extend(
        (
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode()
    )
    body.extend(blob)
    body.extend(f"\r\n--{boundary}--\r\n".encode())

    req = urllib.request.Request(
        f"{base_url}/v1/files",
        data=bytes(body),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="replace"))
    if not data.get("url"):
        raise RuntimeError(f"Upload did not return url: {data}")
    return str(data["url"])


def media_reference(provider: str, base_url: str, api_key: str, blob: bytes, filename: str, mime: str) -> str:
    if provider == "volcengine":
        return to_data_url(blob, filename, mime)
    return upload_file(base_url, api_key, blob, filename, mime)


def to_data_url(blob: bytes, filename: str, fallback: str) -> str:
    mime = mimetypes.guess_type(filename)[0] or fallback
    return f"data:{mime};base64,{base64.b64encode(blob).decode('ascii')}"


def extract_video_url(data: dict[str, Any]) -> str | None:
    content = data.get("content")
    if isinstance(content, dict):
        url = content.get("video_url") or content.get("videoUrl")
        if url:
            return str(url)
    nested = data.get("data")
    if isinstance(nested, dict):
        url = extract_video_url(nested)
        if url:
            return url
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "video_url":
                value = item.get("video_url")
                if isinstance(value, dict) and value.get("url"):
                    return str(value["url"])
                if isinstance(value, str):
                    return value
    for key in ("video_url", "videoUrl", "output"):
        if data.get(key):
            return str(data[key])
    results = data.get("results")
    if isinstance(results, list):
        for item in results:
            if isinstance(item, dict) and item.get("url"):
                return str(item["url"])
    return None


def download_video(url: str, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36"
            )
        },
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        out_path.write_bytes(resp.read())


def set_job(job_id: str, **updates: Any) -> None:
    with JOBS_LOCK:
        JOBS[job_id].update(updates)


def add_event(job_id: str, message: str) -> None:
    with JOBS_LOCK:
        JOBS[job_id].setdefault("events", []).append({"time": time.strftime("%H:%M:%S"), "message": message})


def parse_bool(value: str | None) -> bool:
    return str(value or "").lower() in {"1", "true", "yes", "on"}


def get_field(form: cgi.FieldStorage, name: str, default: str = "") -> str:
    item = form[name] if name in form else None
    if item is None or item.filename:
        return default
    return str(item.value)


def get_file(form: cgi.FieldStorage, name: str) -> tuple[str, bytes] | None:
    item = form[name] if name in form else None
    if item is None or not item.filename:
        return None
    blob = item.file.read()
    if not blob:
        return None
    return Path(item.filename).name, blob


def parse_saved_media(form: cgi.FieldStorage) -> dict[str, Any]:
    raw = get_field(form, "saved_media", "{}")
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def get_file_or_saved(form: cgi.FieldStorage, name: str) -> tuple[str, bytes] | None:
    uploaded = get_file(form, name)
    if uploaded:
        return uploaded
    saved = parse_saved_media(form).get(name)
    if not isinstance(saved, dict):
        return None
    stored = Path(str(saved.get("stored", ""))).name
    if stored:
        path = MEDIA_DIR / stored
        if path.exists():
            return saved.get("filename", path.name), path.read_bytes()
    preset = read_preset()
    item = preset.get("media", {}).get(name)
    if not item:
        return None
    path = MEDIA_DIR / item.get("stored", "")
    if not path.exists():
        return None
    return item.get("filename", path.name), path.read_bytes()


def replace_refs(prompt: str) -> str:
    return re.sub(r"@ref_image(\d+)", r"Image \1", prompt)


def build_payload(form: cgi.FieldStorage, api_key: str, base_url: str, run_index: int) -> dict[str, Any]:
    provider = get_field(form, "provider", "t8star")
    prompt = replace_refs(get_field(form, "prompt").strip())
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    has_frame_input = False
    has_reference_input = False
    has_visual_reference = False

    first = get_file_or_saved(form, "first_frame")
    if first:
        has_frame_input = True
        filename, blob = first
        mime = mimetypes.guess_type(filename)[0] or "image/png"
        url = media_reference(provider, base_url, api_key, blob, filename, mime)
        content.append({"type": "image_url", "image_url": {"url": url}, "role": "first_frame"})

    last = get_file_or_saved(form, "last_frame")
    if last:
        has_frame_input = True
        filename, blob = last
        mime = mimetypes.guess_type(filename)[0] or "image/png"
        url = media_reference(provider, base_url, api_key, blob, filename, mime)
        content.append({"type": "image_url", "image_url": {"url": url}, "role": "last_frame"})

    for i in range(1, 10):
        file_data = get_file_or_saved(form, f"ref_image_{i}")
        if not file_data:
            continue
        has_reference_input = True
        has_visual_reference = True
        filename, blob = file_data
        mime = mimetypes.guess_type(filename)[0] or "image/png"
        url = media_reference(provider, base_url, api_key, blob, filename, mime)
        content.append({"type": "image_url", "image_url": {"url": url}, "role": "reference_image"})

    for i in range(1, 4):
        file_data = get_file_or_saved(form, f"ref_video_{i}")
        if not file_data:
            continue
        has_reference_input = True
        has_visual_reference = True
        filename, blob = file_data
        mime = mimetypes.guess_type(filename)[0] or "video/mp4"
        url = media_reference(provider, base_url, api_key, blob, filename, mime)
        content.append({"type": "video_url", "video_url": {"url": url}, "role": "reference_video"})

    for i in range(1, 4):
        file_data = get_file_or_saved(form, f"ref_audio_{i}")
        if not file_data:
            continue
        has_reference_input = True
        filename, blob = file_data
        mime = mimetypes.guess_type(filename)[0] or "audio/wav"
        url = media_reference(provider, base_url, api_key, blob, filename, mime)
        content.append({"type": "audio_url", "audio_url": {"url": url}, "role": "reference_audio"})

    seed_raw = get_field(form, "seed", "").strip()
    seed = int(seed_raw) if seed_raw else None
    if seed is not None and parse_bool(get_field(form, "vary_seed")):
        seed += run_index

    payload: dict[str, Any] = {
        "model": get_field(form, "custom_model").strip() or get_field(form, "model", "doubao-seedance-2-0-260128"),
        "content": content,
        "duration": int(get_field(form, "duration", "12")),
        "ratio": get_field(form, "ratio", "16:9"),
        "resolution": get_field(form, "resolution", "720p"),
        "generate_audio": parse_bool(get_field(form, "generate_audio")),
        "watermark": parse_bool(get_field(form, "watermark")),
    }
    if seed is not None:
        payload["seed"] = seed
    if parse_bool(get_field(form, "return_last_frame")):
        payload["return_last_frame"] = True
    if parse_bool(get_field(form, "web_search")):
        payload["tools"] = [{"type": "web_search"}]
    if provider == "volcengine":
        if has_frame_input and has_reference_input:
            raise RuntimeError("豆包官方 API 不允许首尾帧与参考图/视频/音频混用，请二选一提交。")
        if has_reference_input and not has_visual_reference:
            raise RuntimeError("豆包官方 API 不允许单独输入参考音频，请至少加入 1 个参考图或参考视频。")
    return payload


def content_counts(payload: dict[str, Any]) -> str:
    counts = {"text": 0, "image_url": 0, "video_url": 0, "audio_url": 0}
    for item in payload.get("content", []):
        if isinstance(item, dict):
            key = item.get("type")
            if key in counts:
                counts[key] += 1
    return ", ".join(f"{k}:{v}" for k, v in counts.items())


def resolve_output_dir(raw: str | None) -> Path:
    if raw and raw.strip():
        path = Path(raw.strip()).expanduser()
    else:
        path = OUTPUT_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def desktop_output_dir() -> str:
    desktop = Path.home() / "Desktop"
    parent = desktop if desktop.exists() else Path.home()
    return str((parent / "seedance_outputs").resolve())


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
        "app": "seedance",
        "endpoints": {
            "schema": "GET /api/schema",
            "submit_json": "POST /api/jobs/json",
            "job_status": "GET /api/jobs/{job_id}",
            "download": "GET /api/download/{token}",
        },
        "providers": config.get("providers", {}),
        "default_provider": config.get("default_provider"),
        "config_error": config_error,
        "value_fields": sorted(VALUE_FIELDS),
        "file_fields": sorted(FILE_FIELDS),
        "media_item": {
            "data_url": "data:image/png;base64,...",
            "url": "https://example.com/file.png",
            "filename": "optional-name.png",
        },
        "example": {
            "provider": "volcengine",
            "model": "doubao-seedance-2-0-260128",
            "prompt": "视频提示词",
            "duration": 8,
            "ratio": "16:9",
            "resolution": "720p",
            "repeat_count": 1,
            "concurrency": 1,
            "media": {
                "ref_image_1": {"data_url": "data:image/png;base64,...", "filename": "ref.png"},
                "ref_video_1": {"url": "https://example.com/ref.mp4"},
                "ref_audio_1": {"url": "https://example.com/ref.mp3"},
            },
        },
    }


def request_template() -> dict[str, Any]:
    config, config_error = load_provider_config()
    provider = str(config.get("default_provider") or "t8star")
    defaults = provider_defaults(config, provider)
    minimal = {
        "api_key": "YOUR_API_KEY",
        "prompt": "describe the video you want",
        "media": {
            "ref_image_1": {
                "filename": "reference.png",
                "data_url": "data:image/png;base64,..."
            }
        },
    }
    media = {
        "first_frame": None,
        "last_frame": None,
        **{f"ref_image_{i}": None for i in range(1, 10)},
        **{f"ref_video_{i}": None for i in range(1, 4)},
        **{f"ref_audio_{i}": None for i in range(1, 4)},
    }
    media["ref_image_1"] = {"filename": "reference.png", "data_url": "data:image/png;base64,..."}
    full = {
        "api_key": "YOUR_API_KEY",
        "provider": provider,
        "base_url": defaults.get("base_url", DEFAULT_BASE_URL),
        "model": defaults.get("model", "doubao-seedance-2-0-260128"),
        "custom_model": "",
        "prompt": "describe the video you want",
        "duration": defaults.get("duration", 12),
        "resolution": defaults.get("resolution", "720p"),
        "ratio": defaults.get("ratio", "16:9"),
        "seed": "",
        "vary_seed": defaults.get("vary_seed", True),
        "generate_audio": False,
        "watermark": False,
        "return_last_frame": False,
        "web_search": False,
        "repeat_count": defaults.get("repeat_count", 1),
        "concurrency": defaults.get("concurrency", 1),
        "poll_interval": defaults.get("poll_interval", 10),
        "timeout": defaults.get("timeout", 3600),
        "media": media,
    }
    return {
        "ok": config_error is None,
        "app": "seedance",
        "endpoint": "POST /api/jobs/json",
        "content_type": "application/json",
        "config_error": config_error,
        "templates": {"minimal": minimal, "full": full},
        "field_notes": {
            "api_key": "可省略；省略时使用本地配置中的 key。",
            "custom_model": "高级字段；非空时覆盖 model 下拉值。",
            "media.*.data_url": "使用 data URL，例如 data:video/mp4;base64,...。",
            "first_frame/last_frame": "首尾帧槽位；豆包官方 API 不允许与参考素材混用。",
            "repeat_count": "请求生成次数；如果 concurrency 更大，后端会按 concurrency 数启动。",
            "concurrency": "同一任务内同时运行的生成数量。",
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


def create_job(values: dict[str, Any], files: dict[str, tuple[str, bytes]], source: str, request_kind: str, request_data: dict[str, Any]) -> str:
    job_id = uuid.uuid4().hex
    activity_id = uuid.uuid4().hex
    with JOBS_LOCK:
        JOBS[job_id] = {"id": job_id, "status": "queued", "events": [], "results": [], "errors": [], "done": 0, "total": 0}
    response = job_id_response(job_id)
    record_activity({
        "id": activity_id,
        "job_id": job_id,
        "source": source,
        "request_kind": request_kind,
        "status": "running",
        "title": str(values.get("prompt") or "")[:80] or "Seedance task",
        "request": request_data,
        "response": response,
        "restore": copy_files_to_restore(values, files, activity_id),
    })
    thread = threading.Thread(target=run_job, args=(job_id, values, files, activity_id), daemon=True)
    thread.start()
    return job_id


def run_one(job_id: str, index: int, form_values: dict[str, Any], form_files: dict[str, tuple[str, bytes]]) -> dict[str, Any]:
    class MemoryForm(dict):
        pass

    form = MemoryForm()
    for key, value in form_values.items():
        form[key] = type("Field", (), {"value": value, "filename": None})()
    for key, (filename, blob) in form_files.items():
        form[key] = type("Field", (), {"filename": filename, "file": type("Reader", (), {"read": lambda self, b=blob: b})()})()

    api_key = str(form_values["api_key"]).strip()
    provider = str(form_values.get("provider") or "t8star")
    default_base = OFFICIAL_ARK_BASE_URL if provider == "volcengine" else DEFAULT_BASE_URL
    base_url = str(form_values.get("base_url") or default_base).rstrip("/")
    create_url = f"{base_url}/contents/generations/tasks" if provider == "volcengine" else f"{base_url}/seedance/v3/contents/generations/tasks"
    add_event(job_id, f"Run {index}: preparing payload")
    payload = build_payload(form, api_key, base_url, index - 1)
    add_event(job_id, f"Run {index}: creating task ({content_counts(payload)})")
    create_result = request_json("POST", create_url, api_key, payload)
    task_id = create_result.get("id") or create_result.get("task_id")
    if not task_id:
        raise RuntimeError(f"No task id returned: {create_result}")
    add_event(job_id, f"Run {index}: task {task_id}")

    status_url = f"{create_url}/{task_id}"
    start = time.time()
    timeout = int(form_values.get("timeout") or 3600)
    poll_interval = int(form_values.get("poll_interval") or 10)
    while True:
        if time.time() - start > timeout:
            raise RuntimeError(f"Task {task_id} timed out after {timeout}s")
        time.sleep(poll_interval)
        status_result = request_json("GET", status_url, api_key, timeout=60)
        status = normalize_status(status_result.get("status"))
        add_event(job_id, f"Run {index}: {status or 'unknown'}")
        if status not in TERMINAL_STATUSES:
            continue
        if status != "succeeded":
            raise RuntimeError(f"Task {task_id} ended as {status}: {status_result}")
        video_url = extract_video_url(status_result)
        if not video_url:
            raise RuntimeError(f"Task {task_id} succeeded but no video URL was found")
        out_dir = resolve_output_dir(form_values.get("output_dir"))
        custom_name = form_values.get("output_name", "").strip()
        if custom_name:
            total = max(1, int(form_values.get("repeat_count") or 1), int(form_values.get("concurrency") or 1))
            if total > 1:
                out_name = f"{custom_name}-{index}.mp4"
            else:
                out_name = f"{custom_name}.mp4"
            if (out_dir / out_name).exists():
                out_name = f"{custom_name}-{index}_{time.strftime('%H%M%S')}.mp4"
        else:
            out_name = f"{time.strftime('%Y%m%d_%H%M%S')}_run{index}_{task_id}.mp4"
        out_path = out_dir / out_name
        download_video(video_url, out_path)
        file_token = uuid.uuid4().hex
        with JOBS_LOCK:
            FILES[file_token] = out_path
        add_event(job_id, f"Run {index}: downloaded {out_name}")
        return {
            "index": index,
            "task_id": task_id,
            "status": "succeeded",
            "video_url": video_url,
            "download_url": f"/api/download/{file_token}",
            "filename": out_name,
            "local_path": str(out_path),
        }


def run_job(job_id: str, form_values: dict[str, Any], form_files: dict[str, tuple[str, bytes]], activity_id: str | None = None) -> None:
    try:
        requested_count = max(1, min(20, int(form_values.get("repeat_count") or 1)))
        requested_concurrency = max(1, min(20, int(form_values.get("concurrency") or 1)))
        count = max(requested_count, requested_concurrency)
        concurrency = min(count, requested_concurrency)
        set_job(job_id, status="running", total=count, done=0, results=[], errors=[])
        add_event(job_id, f"Started {count} run(s), concurrency {concurrency}, key {mask_key(form_values.get('api_key', ''))}")
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [pool.submit(run_one, job_id, i, form_values, form_files) for i in range(1, count + 1)]
            for future in concurrent.futures.as_completed(futures):
                try:
                    result = future.result()
                    with JOBS_LOCK:
                        JOBS[job_id]["results"].append(result)
                        JOBS[job_id]["done"] += 1
                except Exception as exc:
                    with JOBS_LOCK:
                        JOBS[job_id]["errors"].append(str(exc))
                        JOBS[job_id]["done"] += 1
                    add_event(job_id, f"Error: {exc}")
        with JOBS_LOCK:
            errors = JOBS[job_id]["errors"]
            final_job = json.loads(json.dumps(JOBS[job_id]))
        final_status = "failed" if errors else "succeeded"
        set_job(job_id, status=final_status)
        final_job["status"] = final_status
        update_activity(activity_id, status=final_status, result=final_job)
        add_event(job_id, "Finished")
    except Exception as exc:
        set_job(job_id, status="failed", errors=[str(exc)])
        with JOBS_LOCK:
            final_job = json.loads(json.dumps(JOBS.get(job_id, {})))
        update_activity(activity_id, status="failed", error=str(exc), result=final_job)
        add_event(job_id, f"Fatal: {exc}")


class Handler(SimpleHTTPRequestHandler):
    def translate_path(self, path: str) -> str:
        path = urllib.parse.urlparse(path).path
        if path.startswith("/outputs/"):
            return str((OUTPUT_DIR / path.removeprefix("/outputs/")).resolve())
        if path in {"/", "/index.html"}:
            return str(STATIC_DIR / "index.html")
        return str((STATIC_DIR / path.lstrip("/")).resolve())

    def do_GET(self) -> None:
        if self.path == "/api/v1/meta":
            json_response(self, 200, {
                "app": "seedance",
                "version": "1.0.0",
                "port": int(os.environ.get("PORT", "8787")),
                "capabilities": ["text2video", "image2video", "frames2video", "multimodal2video"],
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
            json_response(self, 200, preset_for_client())
            return
        if self.path == "/api/archives":
            json_response(self, 200, {"archives": list_archives()})
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
            preset = read_preset()
            item = preset.get("media", {}).get(field)
            path = MEDIA_DIR / item.get("stored", "") if item else None
            if not item or not path or not path.exists():
                json_response(self, 404, {"error": "media not found"})
                return
            mime = item.get("mime") or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(path.stat().st_size))
            self.end_headers()
            self.wfile.write(path.read_bytes())
            return
        if urllib.parse.urlparse(self.path).path.startswith("/api/media/"):
            raw_name = urllib.parse.urlparse(self.path).path.rsplit("/", 1)[-1]
            stored = Path(urllib.parse.unquote(raw_name)).name
            path = MEDIA_DIR / stored
            if not path.exists():
                json_response(self, 404, {"error": "media not found"})
                return
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(path.stat().st_size))
            self.end_headers()
            self.wfile.write(path.read_bytes())
            return
        if self.path.startswith("/api/jobs/"):
            job_id = self.path.rsplit("/", 1)[-1]
            with JOBS_LOCK:
                job = JOBS.get(job_id)
                data = json.loads(json.dumps(job)) if job else None
            json_response(self, 200 if data else 404, data or {"error": "job not found"})
            return
        if self.path.startswith("/api/download/"):
            token = self.path.rsplit("/", 1)[-1]
            with JOBS_LOCK:
                path = FILES.get(token)
            if not path or not path.exists():
                json_response(self, 404, {"error": "file not found"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
            self.send_header("Content-Length", str(path.stat().st_size))
            self.end_headers()
            self.wfile.write(path.read_bytes())
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
            try:
                json_response(self, 200, {"path": choose_output_dir()})
            except Exception as exc:
                json_response(self, 500, {"error": str(exc)})
            return
        if self.path == "/api/open-output-dir":
            form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={"REQUEST_METHOD": "POST"})
            try:
                json_response(self, 200, {"ok": True, "path": open_output_dir(get_field(form, "output_dir"))})
            except Exception as exc:
                json_response(self, 500, api_error("open_output_dir_failed", "打开输出目录失败", str(exc)))
            return
        if self.path == "/api/cleanup-cache":
            try:
                json_response(self, 200, cleanup_cache())
            except Exception as exc:
                json_response(self, 500, api_error("cleanup_cache_failed", "清理缓存失败", str(exc)))
            return
        if self.path == "/api/workspace/snapshot":
            form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={"REQUEST_METHOD": "POST"})
            try:
                json_response(self, 200, preset_to_client(collect_workspace_snapshot_from_form(form)))
            except Exception as exc:
                json_response(self, 500, {"error": str(exc)})
            return
        if self.path == "/api/preset":
            form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={"REQUEST_METHOD": "POST"})
            preset = collect_preset_from_form(form)
            write_active_preset(preset)
            archive_name = get_field(form, "archive_name")
            archive = None
            if archive_name.strip():
                archive = save_archive_file(archive_name, preset).name
            response = preset_for_client()
            response["archive"] = archive
            response["archives"] = list_archives()
            json_response(self, 200, response)
            return
        if self.path == "/api/archive/load":
            form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={"REQUEST_METHOD": "POST"})
            try:
                data = load_archive_file(get_field(form, "archive_name"))
                data["archives"] = list_archives()
                json_response(self, 200, data)
            except Exception as exc:
                json_response(self, 400, {"error": str(exc)})
            return
        if self.path == "/api/archive/delete":
            form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={"REQUEST_METHOD": "POST"})
            path = archive_path(get_field(form, "archive_name"))
            if path.exists():
                path.unlink()
            json_response(self, 200, {"archives": list_archives()})
            return
        if self.path == "/api/preset/clear":
            if STATE_DIR.exists():
                shutil.rmtree(STATE_DIR)
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
                        "title": str(values.get("prompt") or "")[:80] or "Seedance dry run",
                        "request": summarize_payload(payload),
                        "response": response,
                    })
                    json_response(self, 200, response)
                    return
                request_data = {"raw": summarize_payload(payload), "parsed": summarize_values_files(values, files)}
                job_id = create_job(values, files, "api", "json", request_data)
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

        form_values = {key: get_field(form, key) for key in form.keys() if not getattr(form[key], "filename", None)}
        form_values["api_key"] = api_key
        form_files: dict[str, tuple[str, bytes]] = {}
        for key in form.keys():
            item = form[key]
            if getattr(item, "filename", None):
                blob = item.file.read()
                if blob:
                    form_files[key] = (Path(item.filename).name, blob)

        request_data = summarize_values_files(form_values, form_files)
        job_id = create_job(form_values, form_files, "page", "multipart", request_data)
        json_response(self, 200, job_id_response(job_id))


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    port = int(os.environ.get("PORT", "8787"))
    host = os.environ.get("HOST", "127.0.0.1")
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Seedance GUI running at http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
