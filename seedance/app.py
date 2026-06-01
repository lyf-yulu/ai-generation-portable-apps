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
ARCHIVE_DIR = ROOT / "archives"
DEFAULT_BASE_URL = "https://ai.t8star.cn"
DEFAULT_CONFIG = Path.home() / "ComfyUI/custom_nodes/Comfyui-zhenzhen/Comflyapi.json"
TERMINAL_STATUSES = {"succeeded", "success", "failed", "fail", "failure", "cancelled", "canceled"}

JOBS: dict[str, dict[str, Any]] = {}
FILES: dict[str, Path] = {}
JOBS_LOCK = threading.Lock()

FILE_FIELDS = {
    "first_frame",
    "last_frame",
    *{f"ref_image_{i}" for i in range(1, 10)},
    *{f"ref_video_{i}" for i in range(1, 4)},
    *{f"ref_audio_{i}" for i in range(1, 4)},
}
VALUE_FIELDS = {
    "api_key",
    "base_url",
    "output_dir",
    "model",
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
    "prompt",
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
    handler.end_headers()
    handler.wfile.write(raw)


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


def preset_for_client() -> dict[str, Any]:
    data = read_preset()
    media = {}
    for field, item in data.get("media", {}).items():
        path = MEDIA_DIR / item.get("stored", "")
        if path.exists():
            media[field] = {
                "filename": item.get("filename", path.name),
                "mime": item.get("mime", mimetypes.guess_type(path.name)[0] or "application/octet-stream"),
                "url": f"/api/preset-media/{field}",
            }
    return {"values": data.get("values", {}), "media": media}


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


def collect_preset_from_form(form: cgi.FieldStorage) -> dict[str, Any]:
    preset = read_preset()
    values = {key: get_field(form, key) for key in VALUE_FIELDS if key in form and not getattr(form[key], "filename", None)}
    media = dict(preset.get("media", {}))
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    for key in FILE_FIELDS:
        file_data = get_file(form, key)
        if not file_data:
            continue
        filename, blob = file_data
        suffix = Path(filename).suffix or mimetypes.guess_extension(mimetypes.guess_type(filename)[0] or "") or ".bin"
        stored = f"{key}{suffix}"
        path = MEDIA_DIR / stored
        path.write_bytes(blob)
        media[key] = {
            "filename": filename,
            "stored": stored,
            "mime": mimetypes.guess_type(filename)[0] or "application/octet-stream",
        }
    return {"values": values, "media": media}


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
    if MEDIA_DIR.exists():
        shutil.rmtree(MEDIA_DIR)
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "r") as zf:
        preset = json.loads(zf.read("preset.json").decode("utf-8"))
        for info in zf.infolist():
            if not info.filename.startswith("media/") or info.is_dir():
                continue
            target = MEDIA_DIR / Path(info.filename).name
            target.write_bytes(zf.read(info.filename))
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
    prompt = replace_refs(get_field(form, "prompt").strip())
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]

    first = get_file_or_saved(form, "first_frame")
    if first:
        filename, blob = first
        mime = mimetypes.guess_type(filename)[0] or "image/png"
        url = upload_file(base_url, api_key, blob, filename, mime)
        content.append({"type": "image_url", "image_url": {"url": url}, "role": "first_frame"})

    last = get_file_or_saved(form, "last_frame")
    if last:
        filename, blob = last
        mime = mimetypes.guess_type(filename)[0] or "image/png"
        url = upload_file(base_url, api_key, blob, filename, mime)
        content.append({"type": "image_url", "image_url": {"url": url}, "role": "last_frame"})

    for i in range(1, 10):
        file_data = get_file_or_saved(form, f"ref_image_{i}")
        if not file_data:
            continue
        filename, blob = file_data
        mime = mimetypes.guess_type(filename)[0] or "image/png"
        url = upload_file(base_url, api_key, blob, filename, mime)
        content.append({"type": "image_url", "image_url": {"url": url}, "role": "reference_image"})

    for i in range(1, 4):
        file_data = get_file_or_saved(form, f"ref_video_{i}")
        if not file_data:
            continue
        filename, blob = file_data
        mime = mimetypes.guess_type(filename)[0] or "video/mp4"
        url = upload_file(base_url, api_key, blob, filename, mime)
        content.append({"type": "video_url", "video_url": {"url": url}, "role": "reference_video"})

    for i in range(1, 4):
        file_data = get_file_or_saved(form, f"ref_audio_{i}")
        if not file_data:
            continue
        filename, blob = file_data
        mime = mimetypes.guess_type(filename)[0] or "audio/wav"
        url = upload_file(base_url, api_key, blob, filename, mime)
        content.append({"type": "audio_url", "audio_url": {"url": url}, "role": "reference_audio"})

    seed_raw = get_field(form, "seed", "").strip()
    seed = int(seed_raw) if seed_raw else None
    if seed is not None and parse_bool(get_field(form, "vary_seed")):
        seed += run_index

    payload: dict[str, Any] = {
        "model": get_field(form, "model", "doubao-seedance-2-0-260128"),
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


def run_one(job_id: str, index: int, form_values: dict[str, Any], form_files: dict[str, tuple[str, bytes]]) -> dict[str, Any]:
    class MemoryForm(dict):
        pass

    form = MemoryForm()
    for key, value in form_values.items():
        form[key] = type("Field", (), {"value": value, "filename": None})()
    for key, (filename, blob) in form_files.items():
        form[key] = type("Field", (), {"filename": filename, "file": type("Reader", (), {"read": lambda self, b=blob: b})()})()

    api_key = str(form_values["api_key"]).strip()
    base_url = str(form_values.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
    create_url = f"{base_url}/seedance/v3/contents/generations/tasks"
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


def run_job(job_id: str, form_values: dict[str, Any], form_files: dict[str, tuple[str, bytes]]) -> None:
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
        set_job(job_id, status="failed" if errors else "succeeded")
        add_event(job_id, "Finished")
    except Exception as exc:
        set_job(job_id, status="failed", errors=[str(exc)])
        add_event(job_id, f"Fatal: {exc}")


class Handler(SimpleHTTPRequestHandler):
    def translate_path(self, path: str) -> str:
        if path.startswith("/outputs/"):
            return str((OUTPUT_DIR / path.removeprefix("/outputs/")).resolve())
        if path in {"/", "/index.html"}:
            return str(STATIC_DIR / "index.html")
        return str((STATIC_DIR / path.lstrip("/")).resolve())

    def do_GET(self) -> None:
        if self.path == "/api/config":
            json_response(self, 200, {"has_key": bool(load_default_key()), "masked_key": mask_key(load_default_key())})
            return
        if self.path == "/api/preset":
            json_response(self, 200, preset_for_client())
            return
        if self.path == "/api/archives":
            json_response(self, 200, {"archives": list_archives()})
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

    def do_POST(self) -> None:
        if self.path == "/api/choose-output-dir":
            try:
                json_response(self, 200, {"path": choose_output_dir()})
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
        if self.path != "/api/jobs":
            json_response(self, 404, {"error": "not found"})
            return
        form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={"REQUEST_METHOD": "POST"})
        api_key = get_field(form, "api_key") or load_default_key()
        if not api_key:
            json_response(self, 400, {"error": "API key is required"})
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

        job_id = uuid.uuid4().hex
        with JOBS_LOCK:
            JOBS[job_id] = {"id": job_id, "status": "queued", "events": [], "results": [], "errors": [], "done": 0, "total": 0}
        thread = threading.Thread(target=run_job, args=(job_id, form_values, form_files), daemon=True)
        thread.start()
        json_response(self, 200, {"job_id": job_id})


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    port = int(os.environ.get("PORT", "8787"))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Seedance GUI running at http://127.0.0.1:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
