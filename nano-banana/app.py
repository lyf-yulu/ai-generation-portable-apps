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
import urllib.parse
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

JOBS: dict[str, dict[str, Any]] = {}
FILES: dict[str, Path] = {}
LOCK = threading.Lock()

FILE_FIELDS = {f"image_{i}" for i in range(1, 15)}
VALUE_FIELDS = {
    "api_key", "base_url", "output_dir", "mode", "model", "aspect_ratio", "image_size",
    "response_format", "seed", "control_after_generate", "skip_error", "repeat_count",
    "concurrency", "poll_interval", "timeout", "vary_seed", "prompt", "archive_name",
}


def json_response(handler: SimpleHTTPRequestHandler, status: int, data: dict[str, Any]) -> None:
    raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


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


def request_json(method: str, url: str, api_key: str, body: dict[str, Any] | None = None, timeout: int = 180) -> dict[str, Any]:
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
        raise RuntimeError(f"HTTP {exc.code}: {raw}") from exc


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


def read_preset() -> dict[str, Any]:
    if PRESET_PATH.exists():
        try:
            data = json.loads(PRESET_PATH.read_text(encoding="utf-8"))
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
                "mime": item.get("mime", mimetypes.guess_type(path.name)[0] or "image/png"),
                "url": f"/api/preset-media/{field}",
            }
    return {"values": data.get("values", {}), "media": media}


def parse_saved_media(form: cgi.FieldStorage | dict[str, Any]) -> dict[str, Any]:
    try:
        data = json.loads(get_field(form, "saved_media", "{}"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def get_file_or_saved(form: cgi.FieldStorage | dict[str, Any], name: str) -> tuple[str, bytes] | None:
    uploaded = get_file(form, name)
    if uploaded:
        return uploaded
    if name not in parse_saved_media(form):
        return None
    item = read_preset().get("media", {}).get(name)
    if not item:
        return None
    path = MEDIA_DIR / item.get("stored", "")
    return (item.get("filename", path.name), path.read_bytes()) if path.exists() else None


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
        suffix = Path(filename).suffix or ".png"
        stored = f"{key}{suffix}"
        (MEDIA_DIR / stored).write_bytes(blob)
        media[key] = {"filename": filename, "stored": stored, "mime": mimetypes.guess_type(filename)[0] or "image/png"}
    return {"values": values, "media": media}


def write_active_preset(preset: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    PRESET_PATH.write_text(json.dumps(preset, ensure_ascii=False, indent=2), encoding="utf-8")


def safe_archive_name(raw: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_\-\u4e00-\u9fff]+", "_", (raw or "").strip()).strip("_")
    return name[:80] or time.strftime("nano_banana_%Y%m%d_%H%M%S")


def archive_path(name: str) -> Path:
    return ARCHIVE_DIR / f"{safe_archive_name(name)}.nanobanana"


def list_archives() -> list[dict[str, Any]]:
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    return [{"name": p.stem, "filename": p.name, "size": p.stat().st_size, "updated_at": int(p.stat().st_mtime)}
            for p in sorted(ARCHIVE_DIR.glob("*.nanobanana"), key=lambda x: x.stat().st_mtime, reverse=True)]


def save_archive_file(name: str, preset: dict[str, Any]) -> Path:
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    path = archive_path(name)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("preset.json", json.dumps(preset, ensure_ascii=False, indent=2))
        for _, item in preset.get("media", {}).items():
            src = MEDIA_DIR / item.get("stored", "")
            if src.exists():
                zf.write(src, f"media/{src.name}")
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
            if info.filename.startswith("media/") and not info.is_dir():
                (MEDIA_DIR / Path(info.filename).name).write_bytes(zf.read(info.filename))
    write_active_preset(preset)
    return preset_for_client()


def choose_output_dir() -> str:
    prompt = "选择 Nano Banana 输出目录"
    if sys.platform == "darwin":
        result = subprocess.run(
            ["osascript", "-e", f'POSIX path of (choose folder with prompt "{prompt}")'],
            check=True,
            capture_output=True,
            text=True,
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
    with urllib.request.urlopen(req, timeout=300) as resp:
        out_path.write_bytes(resp.read())


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


def run_one(job_id: str, index: int, values: dict[str, Any], files: dict[str, tuple[str, bytes]]) -> dict[str, Any]:
    form = build_form(values, files)
    api_key = str(values["api_key"]).strip()
    base_url = str(values.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
    mode = get_field(form, "mode", "img2img")
    seed_raw = get_field(form, "seed", "").strip()
    seed = int(seed_raw) if seed_raw else 0
    if seed > 0 and get_field(form, "vary_seed", "") in {"on", "true", "1"}:
        seed += index - 1

    common = {
        "prompt": get_field(form, "prompt").strip(),
        "model": get_field(form, "model", "nano-banana-2"),
        "aspect_ratio": get_field(form, "aspect_ratio", "auto"),
        "response_format": get_field(form, "response_format", "url"),
    }
    image_size = get_field(form, "image_size", "2K")
    if image_size:
        common["image_size"] = image_size
    if seed > 0:
        common["seed"] = str(seed)

    add_event(job_id, f"Run {index}: submitting {mode}")
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
            file_data = get_file_or_saved(form, f"image_{i}")
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
            if state in {"failed", "failure", "error"}:
                raise RuntimeError(f"Task {task_id} failed: {status}")

    items = extract_items(final)
    if not items:
        raise RuntimeError(f"No image result found: {final}")
    out_dir = resolve_output_dir(values.get("output_dir"))
    file_token_results = []
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
    return {"index": index, "task_id": task_id, "status": "succeeded", "images": file_token_results}


def run_job(job_id: str, values: dict[str, Any], files: dict[str, tuple[str, bytes]]) -> None:
    try:
        requested_count = max(1, min(50, int(values.get("repeat_count") or 1)))
        requested_concurrency = max(1, min(20, int(values.get("concurrency") or 1)))
        count = max(requested_count, requested_concurrency)
        concurrency = min(count, requested_concurrency)
        set_job(job_id, status="running", total=count, done=0, results=[], errors=[])
        add_event(job_id, f"Started {count} run(s), concurrency {concurrency}, key {mask_key(values.get('api_key', ''))}")
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [pool.submit(run_one, job_id, i, values, files) for i in range(1, count + 1)]
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
        set_job(job_id, status="failed" if errors else "succeeded")
        add_event(job_id, "Finished")
    except Exception as exc:
        set_job(job_id, status="failed", errors=[str(exc)])
        add_event(job_id, f"Fatal: {exc}")


class Handler(SimpleHTTPRequestHandler):
    def translate_path(self, path: str) -> str:
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
        if self.path.startswith("/api/preset-media/"):
            field = self.path.rsplit("/", 1)[-1]
            item = read_preset().get("media", {}).get(field)
            path = MEDIA_DIR / item.get("stored", "") if item else None
            if not item or not path or not path.exists():
                json_response(self, 404, {"error": "media not found"})
                return
            mime = item.get("mime") or "image/png"
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(path.stat().st_size))
            self.end_headers()
            self.wfile.write(path.read_bytes())
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
            self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "image/png")
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
            archive = save_archive_file(archive_name, preset).name if archive_name.strip() else None
            data = preset_for_client()
            data["archive"] = archive
            data["archives"] = list_archives()
            json_response(self, 200, data)
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
        values = {key: get_field(form, key) for key in form.keys() if not getattr(form[key], "filename", None)}
        values["api_key"] = api_key
        files = {}
        for key in form.keys():
            item = form[key]
            if getattr(item, "filename", None):
                blob = item.file.read()
                if blob:
                    files[key] = (Path(item.filename).name, blob)
        job_id = uuid.uuid4().hex
        with LOCK:
            JOBS[job_id] = {"id": job_id, "status": "queued", "events": [], "results": [], "errors": [], "done": 0, "total": 0}
        threading.Thread(target=run_job, args=(job_id, values, files), daemon=True).start()
        json_response(self, 200, {"job_id": job_id})


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    port = int(os.environ.get("PORT", "8797"))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Nano Banana GUI running at http://127.0.0.1:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
