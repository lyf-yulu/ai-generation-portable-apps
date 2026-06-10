#!/usr/bin/env python3
from __future__ import annotations

import concurrent.futures
import json
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
import webbrowser
import zipfile
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
OUTPUT_DIR = ROOT / "outputs"
UPLOAD_DIR = ROOT / "uploads"
LOG_DIR = ROOT / "logs"
STATE_DIR = ROOT / "state"
ARCHIVE_DIR = ROOT / "archives"
MEDIA_DIR = STATE_DIR / "media"
PRESET_PATH = STATE_DIR / "preset.json"
HISTORY_PATH = STATE_DIR / "history.json"
CONFIG_PATH = ROOT / "config.json"

APP_VERSION = "0.2.0"

DEFAULT_CONFIG = {
    "port": 8888,
    "host": "127.0.0.1",
    "max_concurrent": 5,
    "poll_image": 60,
    "poll_video": 300,
    "login_timeout": 120,
    "upload_max_age_days": 7,
    "cors": False,
}

JOBS: dict[str, dict[str, Any]] = {}
LOCK = threading.Lock()
LOGIN_PROC: subprocess.Popen | None = None
LOGIN_LOCK = threading.Lock()
EXECUTOR: concurrent.futures.ThreadPoolExecutor | None = None


def load_config() -> dict[str, Any]:
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text("utf-8"))
            merged = {**DEFAULT_CONFIG, **cfg}
            return merged
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)


def ensure_dirs():
    for d in (OUTPUT_DIR, UPLOAD_DIR, LOG_DIR, STATE_DIR, ARCHIVE_DIR, MEDIA_DIR):
        d.mkdir(parents=True, exist_ok=True)


def cleanup_old_uploads():
    cfg = load_config()
    max_age = cfg.get("upload_max_age_days", 7) * 86400
    now = time.time()
    if not UPLOAD_DIR.exists():
        return
    for f in UPLOAD_DIR.iterdir():
        if f.is_file() and (now - f.stat().st_mtime) > max_age:
            f.unlink(missing_ok=True)


def find_available_port(start: int) -> int:
    for port in range(start, start + 100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return start + 100


def run_cmd(args: list[str], timeout: int = 30) -> dict[str, Any]:
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout
        )
        return {
            "returncode": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        }
    except subprocess.TimeoutExpired:
        return {"returncode": -1, "stdout": "", "stderr": "timeout"}
    except FileNotFoundError:
        return {"returncode": -1, "stdout": "", "stderr": "command not found"}


def check_cli_installed() -> bool:
    r = run_cmd(["which", "dreamina"], timeout=5)
    if r["returncode"] == 0:
        return True
    return Path.home().joinpath(".dreamina_cli").exists()


def check_login() -> dict[str, Any]:
    r = run_cmd(["dreamina", "user_credit"], timeout=15)
    if r["returncode"] != 0:
        return {"logged_in": False, "credit": None, "raw": r["stderr"]}
    try:
        data = json.loads(r["stdout"])
        return {"logged_in": True, "credit": data}
    except json.JSONDecodeError:
        if "credit" in r["stdout"].lower() or "{" in r["stdout"]:
            return {"logged_in": True, "credit": r["stdout"]}
        return {"logged_in": False, "credit": None, "raw": r["stdout"]}


def read_history() -> list[dict[str, Any]]:
    if not HISTORY_PATH.exists():
        return []
    try:
        return json.loads(HISTORY_PATH.read_text("utf-8"))
    except Exception:
        return []


def write_history(items: list[dict[str, Any]]):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_PATH.write_text(json.dumps(items[-500:], ensure_ascii=False, indent=2), "utf-8")


def record_job(job: dict[str, Any]):
    items = read_history()
    items.append(job)
    write_history(items)


def update_job_in_history(job_id: str, updates: dict[str, Any]):
    items = read_history()
    for item in items:
        if item.get("job_id") == job_id:
            item.update(updates)
            break
    write_history(items)


def execute_task(job_id: str, task_type: str, args: list[str], params: dict[str, Any]):
    with LOCK:
        job = JOBS[job_id]
        job["status"] = "running"

    total = job.get("total", 1)
    concurrency = job.get("concurrency", 1)

    def add_event(msg: str):
        with LOCK:
            job["events"].append({"time": time.strftime("%H:%M:%S"), "message": msg})

    def run_one(index: int):
        add_event(f"子任务 {index}/{total} 开始")
        result = run_cmd(args, timeout=params.get("timeout", 600))
        with LOCK:
            job["done"] += 1
        if result["returncode"] == 0:
            try:
                data = json.loads(result["stdout"])
            except json.JSONDecodeError:
                data = {"raw": result["stdout"]}
            submit_id = data.get("submit_id") or ""
            if submit_id:
                dl = download_if_needed(submit_id, data, task_type, job_id,
                                        output_name=job.get("output_name", ""),
                                        sub_index=index, total=total)
                if dl:
                    with LOCK:
                        job["results"].append(dl)
                    add_event(f"子任务 {index}/{total} 完成")
                    return
            with LOCK:
                job["results"].append(data)
            add_event(f"子任务 {index}/{total} 完成")
        else:
            error_msg = result["stderr"] or result["stdout"] or "unknown error"
            with LOCK:
                job["errors"].append(f"[{index}] {error_msg}")
            add_event(f"子任务 {index}/{total} 失败: {error_msg[:80]}")

    if total <= 1:
        run_one(1)
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [pool.submit(run_one, i) for i in range(1, total + 1)]
            concurrent.futures.wait(futures)

    with LOCK:
        job["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        if job["errors"] and not job["results"]:
            job["status"] = "failed"
            job["error"] = "; ".join(job["errors"][:3])
            job["retryable"] = True
        else:
            job["status"] = "completed"
            if len(job["results"]) == 1:
                job["result"] = job["results"][0]
            else:
                all_files = []
                for r in job["results"]:
                    if isinstance(r, dict):
                        all_files.extend(r.get("files", []))
                job["result"] = {"files": all_files, "count": len(job["results"])}

    with LOCK:
        final_job = dict(JOBS[job_id])
    update_job_in_history(job_id, {
        "status": final_job["status"],
        "result": final_job.get("result"),
        "error": final_job.get("error"),
        "finished_at": final_job.get("finished_at"),
        "done": final_job.get("done"),
        "total": final_job.get("total"),
    })


def choose_output_dir() -> str:
    prompt = "选择 Dreamina 输出目录"
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
            check=True, capture_output=True, text=True,
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
    if raw and raw.strip():
        path = Path(raw.strip()).expanduser()
    else:
        path = OUTPUT_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def desktop_output_dir() -> str:
    desktop = Path.home() / "Desktop"
    parent = desktop if desktop.exists() else Path.home()
    return str((parent / "dreamina_outputs").resolve())


def open_output_dir(raw: str | None) -> str:
    path = resolve_output_dir(raw)
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    elif sys.platform.startswith("win"):
        os.startfile(str(path))
    else:
        subprocess.Popen(["xdg-open", str(path)])
    return str(path)


def cleanup_cache(media_days: int = 30, log_days: int = 14) -> dict[str, Any]:
    now = time.time()
    media_cutoff = now - max(1, media_days) * 86400
    log_cutoff = now - max(1, log_days) * 86400
    stats = {
        "ok": True,
        "media_deleted": 0,
        "logs_deleted": 0,
        "bytes_deleted": 0,
    }
    if UPLOAD_DIR.exists():
        for path in UPLOAD_DIR.iterdir():
            if not path.is_file() or path.stat().st_mtime >= media_cutoff:
                continue
            size = path.stat().st_size
            path.unlink()
            stats["media_deleted"] += 1
            stats["bytes_deleted"] += size
    if LOG_DIR.exists():
        for path in LOG_DIR.iterdir():
            if not path.is_file() or path.stat().st_mtime >= log_cutoff:
                continue
            size = path.stat().st_size
            path.unlink()
            stats["logs_deleted"] += 1
            stats["bytes_deleted"] += size
    return stats


def download_if_needed(submit_id: str, data: dict, task_type: str, job_id: str, output_name: str = "", sub_index: int = 1, total: int = 1) -> dict | None:
    if not submit_id:
        return None
    ts = time.strftime("%Y%m%d_%H%M%S")
    short_id = job_id[:8]
    custom_name = (output_name or "").strip()
    if custom_name:
        if total > 1:
            dl_dir = OUTPUT_DIR / f"{custom_name}-{sub_index}"
        else:
            dl_dir = OUTPUT_DIR / custom_name
        if dl_dir.exists():
            dl_dir = OUTPUT_DIR / f"{custom_name}-{sub_index}_{ts}"
    else:
        dl_dir = OUTPUT_DIR / f"{ts}_{task_type}_{short_id}"
    dl_dir.mkdir(parents=True, exist_ok=True)

    r = run_cmd(["dreamina", "query_result", f"--submit_id={submit_id}", f"--download_dir={dl_dir}"], timeout=60)
    if r["returncode"] == 0:
        files = [str(f.relative_to(ROOT)) for f in dl_dir.iterdir() if f.is_file()]
        return {"download_dir": str(dl_dir.relative_to(ROOT)), "files": files, "cli_output": r["stdout"]}
    return {"download_dir": str(dl_dir.relative_to(ROOT)), "files": [], "cli_output": data}


def json_response(handler, status: int, data: dict):
    cfg = load_config()
    raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    if cfg.get("cors"):
        handler.send_header("Access-Control-Allow-Origin", "*")
        handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        handler.send_header("Access-Control-Allow-Headers", "Content-Type")
    handler.end_headers()
    handler.wfile.write(raw)


def read_json_body(handler, max_bytes: int = 50 * 1024 * 1024) -> dict:
    length = int(handler.headers.get("Content-Length") or "0")
    if length <= 0:
        return {}
    if length > max_bytes:
        raise ValueError("body too large")
    return json.loads(handler.rfile.read(length).decode("utf-8"))


def parse_multipart(handler) -> tuple[dict[str, str], dict[str, tuple[str, bytes]]]:
    content_type = handler.headers.get("Content-Type", "")
    boundary_match = re.search(r"boundary=(.+)", content_type)
    if not boundary_match:
        raise ValueError("no boundary")
    boundary = boundary_match.group(1).encode()
    length = int(handler.headers.get("Content-Length", "0"))
    body = handler.rfile.read(length)

    fields: dict[str, str] = {}
    files: dict[str, tuple[str, bytes]] = {}

    parts = body.split(b"--" + boundary)
    for part in parts[1:]:
        if part.strip() in (b"", b"--", b"--\r\n"):
            continue
        header_end = part.find(b"\r\n\r\n")
        if header_end < 0:
            continue
        headers_raw = part[:header_end].decode("utf-8", errors="replace")
        content = part[header_end + 4:]
        if content.endswith(b"\r\n"):
            content = content[:-2]

        name_match = re.search(r'name="([^"]+)"', headers_raw)
        filename_match = re.search(r'filename="([^"]*)"', headers_raw)
        if not name_match:
            continue
        name = name_match.group(1)
        if filename_match and filename_match.group(1):
            files[name] = (filename_match.group(1), content)
        else:
            fields[name] = content.decode("utf-8", errors="replace")

    return fields, files


def save_uploaded_files(files: dict[str, tuple[str, bytes]], prefix: str) -> list[Path]:
    """Save all files whose key starts with prefix, return sorted paths."""
    saved = []
    for key in sorted(files.keys()):
        if key.startswith(prefix):
            filename, blob = files[key]
            suffix = Path(filename).suffix or ".bin"
            stored = UPLOAD_DIR / f"{uuid.uuid4().hex}{suffix}"
            stored.write_bytes(blob)
            saved.append(stored)
    return saved


# === Archive / Preset Helpers ===

def sanitize_archive_name(name: str) -> str:
    clean = re.sub(r'[^\w一-鿿\-]', '_', name).strip('_')
    return clean or time.strftime("%Y%m%d_%H%M%S")


def read_preset() -> dict[str, Any]:
    if not PRESET_PATH.exists():
        return {"values": {}, "media": {}}
    try:
        return json.loads(PRESET_PATH.read_text("utf-8"))
    except Exception:
        return {"values": {}, "media": {}}


def write_preset(data: dict[str, Any]):
    PRESET_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")


def preset_for_client() -> dict[str, Any]:
    data = read_preset()
    media = {}
    for field, item in data.get("media", {}).items():
        path = MEDIA_DIR / item.get("stored", "")
        if path.exists():
            media[field] = {
                "filename": item.get("filename", path.name),
                "url": f"/api/preset-media/{field}",
            }
    return {"values": data.get("values", {}), "media": media, "archives": list_archives()}


def list_archives() -> list[dict[str, Any]]:
    if not ARCHIVE_DIR.exists():
        return []
    archives = []
    for f in sorted(ARCHIVE_DIR.iterdir()):
        if f.suffix == ".dreamina" and f.is_file():
            archives.append({"name": f.stem, "size": f.stat().st_size, "mtime": f.stat().st_mtime})
    return archives


def save_archive(name: str, preset: dict[str, Any]):
    safe_name = sanitize_archive_name(name)
    path = ARCHIVE_DIR / f"{safe_name}.dreamina"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("preset.json", json.dumps(preset, ensure_ascii=False, indent=2))
        for field, item in preset.get("media", {}).items():
            src = MEDIA_DIR / item.get("stored", "")
            if src.exists():
                zf.write(src, f"media/{item['stored']}")
    return safe_name


def load_archive(name: str) -> dict[str, Any] | None:
    path = ARCHIVE_DIR / f"{name}.dreamina"
    if not path.exists():
        return None
    if MEDIA_DIR.exists():
        shutil.rmtree(MEDIA_DIR)
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "r") as zf:
        preset = json.loads(zf.read("preset.json").decode("utf-8"))
        for info in zf.infolist():
            if info.filename.startswith("media/") and not info.is_dir():
                stored_name = info.filename[len("media/"):]
                target = MEDIA_DIR / stored_name
                target.write_bytes(zf.read(info.filename))
    write_preset(preset)
    return preset_for_client()


def delete_archive(name: str) -> bool:
    path = ARCHIVE_DIR / f"{name}.dreamina"
    if path.exists():
        path.unlink()
        return True
    return False


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _client_ip(self) -> str:
        forwarded = self.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return self.client_address[0]

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/api/env/check":
            self.handle_env_check()
        elif path == "/api/env/login-poll":
            self.handle_login_poll()
        elif path == "/api/jobs":
            self.handle_jobs_list()
        elif path.startswith("/api/jobs/"):
            job_id = path.split("/api/jobs/")[1].split("/")[0]
            self.handle_job_status(job_id)
        elif path == "/api/history":
            self.handle_history()
        elif path == "/api/preset":
            json_response(self, 200, {"ok": True, **preset_for_client()})
        elif path == "/api/archives":
            json_response(self, 200, {"ok": True, "archives": list_archives()})
        elif path.startswith("/api/preset-media/"):
            field = path[len("/api/preset-media/"):]
            self.handle_preset_media(field)
        elif path == "/api/v1/meta":
            self.handle_meta()
        elif path == "/api/default-output-dir":
            json_response(self, 200, {"path": desktop_output_dir()})
        elif path.startswith("/outputs/"):
            self.serve_file(OUTPUT_DIR, path[len("/outputs/"):])
        elif path.startswith("/uploads/"):
            self.serve_file(UPLOAD_DIR, path[len("/uploads/"):])
        else:
            self.serve_static(path)

    def do_OPTIONS(self):
        cfg = load_config()
        self.send_response(204)
        if cfg.get("cors"):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Max-Age", "86400")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path

        if path == "/api/env/install-cli":
            self.handle_install_cli()
        elif path == "/api/env/login":
            self.handle_login()
        elif path == "/api/env/login-cancel":
            self.handle_login_cancel()
        elif path == "/api/env/switch-account":
            self.handle_switch_account()
        elif path == "/api/env/update-cli":
            self.handle_install_cli()
        elif path == "/api/text2image":
            self.handle_generate("text2image")
        elif path == "/api/image2image":
            self.handle_generate("image2image")
        elif path == "/api/text2video":
            self.handle_generate("text2video")
        elif path == "/api/image2video":
            self.handle_generate("image2video")
        elif path == "/api/frames2video":
            self.handle_generate("frames2video")
        elif path == "/api/multimodal2video":
            self.handle_generate("multimodal2video")
        elif path == "/api/multiframe2video":
            self.handle_generate("multiframe2video")
        elif path.startswith("/api/jobs/") and path.endswith("/retry"):
            job_id = path.split("/api/jobs/")[1].split("/")[0]
            self.handle_retry(job_id)
        elif path == "/api/cache/clean":
            self.handle_cache_clean()
        elif path == "/api/choose-output-dir":
            try:
                json_response(self, 200, {"path": choose_output_dir()})
            except Exception as exc:
                json_response(self, 500, {"ok": False, "error": str(exc)})
        elif path == "/api/open-output-dir":
            try:
                ct = self.headers.get("Content-Type", "")
                output_dir = None
                if "json" in ct:
                    body = read_json_body(self)
                    output_dir = body.get("output_dir")
                elif "multipart" in ct:
                    fields, _ = parse_multipart(self)
                    output_dir = fields.get("output_dir")
                else:
                    length = int(self.headers.get("Content-Length") or "0")
                    raw = self.rfile.read(length).decode("utf-8", errors="replace") if length > 0 else ""
                    for part in raw.split("&"):
                        if part.startswith("output_dir="):
                            output_dir = urllib.parse.unquote_plus(part[len("output_dir="):])
                json_response(self, 200, {"ok": True, "path": open_output_dir(output_dir)})
            except Exception as exc:
                json_response(self, 500, {"ok": False, "error": str(exc)})
        elif path == "/api/cleanup-cache":
            try:
                json_response(self, 200, cleanup_cache())
            except Exception as exc:
                json_response(self, 500, {"ok": False, "error": str(exc)})
        elif path == "/api/preset":
            self.handle_preset_save()
        elif path == "/api/preset/clear":
            self.handle_preset_clear()
        elif path == "/api/archive/load":
            self.handle_archive_load()
        elif path == "/api/archive/delete":
            self.handle_archive_delete()
        elif path == "/api/archive/from-history":
            self.handle_archive_from_history()
        else:
            json_response(self, 404, {"ok": False, "error": "not found"})

    def handle_env_check(self):
        installed = check_cli_installed()
        login_info = check_login() if installed else {"logged_in": False, "credit": None}
        json_response(self, 200, {
            "ok": True,
            "cli_installed": installed,
            "logged_in": login_info["logged_in"],
            "credit": login_info.get("credit"),
        })

    def handle_login_poll(self):
        info = check_login()
        json_response(self, 200, {"ok": True, **info})

    def handle_install_cli(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        def send_event(data: str):
            self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
            self.wfile.flush()

        try:
            proc = subprocess.Popen(
                ["bash", "-c", "curl -fsSL https://jimeng.jianying.com/cli | bash"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
            )
            for line in proc.stdout:
                send_event(json.dumps({"type": "log", "text": line.rstrip()}))
            proc.wait()
            if proc.returncode == 0:
                send_event(json.dumps({"type": "done", "success": True}))
            else:
                send_event(json.dumps({"type": "done", "success": False, "error": f"exit code {proc.returncode}"}))
        except Exception as e:
            send_event(json.dumps({"type": "done", "success": False, "error": str(e)}))

    def handle_login(self):
        global LOGIN_PROC
        with LOGIN_LOCK:
            if LOGIN_PROC and LOGIN_PROC.poll() is None:
                json_response(self, 200, {"ok": True, "message": "login already in progress"})
                return
            try:
                LOGIN_PROC = subprocess.Popen(
                    ["dreamina", "login"],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, start_new_session=True,
                )
            except FileNotFoundError:
                json_response(self, 400, {"ok": False, "error": "dreamina not found"})
                return

        cfg = load_config()
        timeout = cfg.get("login_timeout", 120)
        auth_url = ""

        def read_output_and_open_browser():
            nonlocal auth_url
            try:
                for line in LOGIN_PROC.stdout:
                    if "verification_uri:" in line:
                        auth_url = line.split("verification_uri:", 1)[1].strip()
                        webbrowser.open(auth_url)
                        break
            except Exception:
                pass

        threading.Thread(target=read_output_and_open_browser, daemon=True).start()

        def kill_after_timeout():
            time.sleep(timeout)
            with LOGIN_LOCK:
                if LOGIN_PROC and LOGIN_PROC.poll() is None:
                    LOGIN_PROC.kill()

        threading.Thread(target=kill_after_timeout, daemon=True).start()

        time.sleep(2)
        json_response(self, 200, {"ok": True, "message": "login started", "timeout": timeout, "auth_url": auth_url})

    def handle_login_cancel(self):
        global LOGIN_PROC
        with LOGIN_LOCK:
            if LOGIN_PROC and LOGIN_PROC.poll() is None:
                LOGIN_PROC.kill()
                LOGIN_PROC = None
        json_response(self, 200, {"ok": True, "message": "login cancelled"})

    def handle_switch_account(self):
        r = run_cmd(["dreamina", "relogin"], timeout=5)
        global LOGIN_PROC
        with LOGIN_LOCK:
            try:
                LOGIN_PROC = subprocess.Popen(
                    ["dreamina", "login"],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, start_new_session=True,
                )
            except FileNotFoundError:
                json_response(self, 400, {"ok": False, "error": "dreamina not found"})
                return

        cfg = load_config()
        timeout = cfg.get("login_timeout", 120)
        auth_url = ""

        def read_output_and_open_browser():
            nonlocal auth_url
            try:
                for line in LOGIN_PROC.stdout:
                    if "verification_uri:" in line:
                        auth_url = line.split("verification_uri:", 1)[1].strip()
                        webbrowser.open(auth_url)
                        break
            except Exception:
                pass

        threading.Thread(target=read_output_and_open_browser, daemon=True).start()

        def kill_after_timeout():
            time.sleep(timeout)
            with LOGIN_LOCK:
                if LOGIN_PROC and LOGIN_PROC.poll() is None:
                    LOGIN_PROC.kill()

        threading.Thread(target=kill_after_timeout, daemon=True).start()

        time.sleep(2)
        json_response(self, 200, {"ok": True, "message": "switch account started", "timeout": timeout, "auth_url": auth_url})

    def handle_generate(self, task_type: str):
        cfg = load_config()
        running = sum(1 for j in JOBS.values() if j["status"] in ("pending", "running"))
        if running >= cfg["max_concurrent"]:
            json_response(self, 429, {"ok": False, "error": "max concurrent reached", "max": cfg["max_concurrent"]})
            return

        content_type = self.headers.get("Content-Type", "")
        if "multipart" in content_type:
            fields, files = parse_multipart(self)
        else:
            fields = read_json_body(self)
            files = {}

        prompt = fields.get("prompt", "")
        if not prompt and task_type not in ("multimodal2video",):
            json_response(self, 400, {"ok": False, "error": "prompt is required"})
            return

        uploaded_paths = {}
        if files:
            uploaded_paths["ref_image"] = save_uploaded_files(files, "ref_image_")
            uploaded_paths["ref_video"] = save_uploaded_files(files, "ref_video_")
            uploaded_paths["ref_audio"] = save_uploaded_files(files, "ref_audio_")
            uploaded_paths["first_frame"] = save_uploaded_files(files, "first_frame")
            uploaded_paths["last_frame"] = save_uploaded_files(files, "last_frame")
            uploaded_paths["frame_"] = save_uploaded_files(files, "frame_")
            if not any(uploaded_paths.values()):
                legacy = save_uploaded_files(files, "image")
                if legacy:
                    uploaded_paths["ref_image"] = legacy

        args = self.build_cli_args(task_type, fields, uploaded_paths, cfg)
        poll_timeout = cfg["poll_video"] if "video" in task_type else cfg["poll_image"]
        total_timeout = poll_timeout + 30

        repeat_count = max(1, min(10, int(fields.get("repeat_count") or 1)))
        concurrency_val = max(1, min(5, int(fields.get("concurrency") or 1)))
        total = max(repeat_count, concurrency_val)

        job_id = uuid.uuid4().hex
        job = {
            "job_id": job_id,
            "task_type": task_type,
            "status": "pending",
            "total": total,
            "done": 0,
            "concurrency": concurrency_val,
            "output_name": fields.get("output_name", ""),
            "client_ip": self._client_ip(),
            "events": [],
            "results": [],
            "errors": [],
            "params": {k: v for k, v in fields.items()},
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "finished_at": None,
            "result": None,
            "error": None,
            "retryable": False,
        }

        with LOCK:
            JOBS[job_id] = job

        record_job(job)
        EXECUTOR.submit(execute_task, job_id, task_type, args, {"timeout": total_timeout})

        json_response(self, 200, {"ok": True, "job_id": job_id})

    def build_cli_args(self, task_type: str, fields: dict, uploaded_paths: dict, cfg: dict) -> list[str]:
        prompt = fields.get("prompt", "")
        ratio = fields.get("ratio", "1:1")
        resolution = fields.get("resolution_type", "2k")
        duration = fields.get("duration", "5")
        video_resolution = fields.get("video_resolution", "720P")
        model_version = fields.get("model_version", "seedance2.0fast")
        poll = cfg["poll_video"] if "video" in task_type else cfg["poll_image"]

        if task_type == "text2image":
            return ["dreamina", "text2image", f"--prompt={prompt}", f"--ratio={ratio}", f"--resolution_type={resolution}", f"--poll={poll}"]

        elif task_type == "image2image":
            images = uploaded_paths.get("ref_image", [])
            img_str = ",".join(str(p) for p in images) if images else ""
            return ["dreamina", "image2image", "--images", img_str, f"--prompt={prompt}", f"--ratio={ratio}", f"--resolution_type={resolution}", f"--poll={poll}"]

        elif task_type == "text2video":
            return ["dreamina", "text2video", f"--prompt={prompt}", f"--duration={duration}", f"--ratio={ratio}", f"--video_resolution={video_resolution}", f"--poll={poll}"]

        elif task_type == "image2video":
            images = uploaded_paths.get("ref_image", [])
            img = str(images[0]) if images else ""
            return ["dreamina", "image2video", "--image", img, f"--prompt={prompt}", f"--duration={duration}", f"--poll={poll}"]

        elif task_type == "frames2video":
            first_list = uploaded_paths.get("first_frame", [])
            last_list = uploaded_paths.get("last_frame", [])
            first = str(first_list[0]) if first_list else ""
            last = str(last_list[0]) if last_list else ""
            args = ["dreamina", "frames2video", "--first", first, "--last", last, f"--prompt={prompt}", f"--duration={duration}", f"--video_resolution={video_resolution}", f"--poll={poll}"]
            if model_version:
                args.append(f"--model_version={model_version}")
            return args

        elif task_type == "multimodal2video":
            args = ["dreamina", "multimodal2video", f"--prompt={prompt}", f"--duration={duration}", f"--ratio={ratio}", f"--video_resolution={video_resolution}", f"--model_version={model_version}", f"--poll={poll}"]
            for img in uploaded_paths.get("ref_image", []):
                args.extend(["--image", str(img)])
            for vid in uploaded_paths.get("ref_video", []):
                args.extend(["--video", str(vid)])
            for aud in uploaded_paths.get("ref_audio", []):
                args.extend(["--audio", str(aud)])
            return args

        elif task_type == "multiframe2video":
            frames = uploaded_paths.get("frame_", [])
            img_str = ",".join(str(p) for p in frames) if frames else ""
            args = ["dreamina", "multiframe2video", "--images", img_str, f"--poll={poll}"]
            idx = 1
            while f"transition_prompt_{idx}" in fields:
                args.extend(["--transition-prompt", fields[f"transition_prompt_{idx}"]])
                idx += 1
            idx = 1
            while f"transition_duration_{idx}" in fields:
                args.extend(["--transition-duration", fields[f"transition_duration_{idx}"]])
                idx += 1
            return args

        return []

    def handle_retry(self, job_id: str):
        with LOCK:
            job = JOBS.get(job_id)
        if not job:
            json_response(self, 404, {"ok": False, "error": "job not found"})
            return
        if job["status"] not in ("failed",):
            json_response(self, 400, {"ok": False, "error": "job is not failed"})
            return

        cfg = load_config()
        task_type = job["task_type"]
        fields = job.get("params", {})
        args = self.build_cli_args(task_type, fields, None, cfg)
        poll = cfg["poll_video"] if "video" in task_type else cfg["poll_image"]

        new_job_id = uuid.uuid4().hex
        new_job = {
            "job_id": new_job_id,
            "task_type": task_type,
            "status": "pending",
            "params": fields,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "finished_at": None,
            "result": None,
            "error": None,
            "retryable": False,
        }
        with LOCK:
            JOBS[new_job_id] = new_job
        record_job(new_job)
        EXECUTOR.submit(execute_task, new_job_id, task_type, args, {"timeout": poll + 30})
        json_response(self, 200, {"ok": True, "job_id": new_job_id})

    def handle_jobs_list(self):
        ip = self._client_ip()
        with LOCK:
            jobs = [j for j in JOBS.values() if j.get("client_ip", "") in ("", ip)]
        json_response(self, 200, {"ok": True, "jobs": jobs})

    def handle_job_status(self, job_id: str):
        with LOCK:
            job = JOBS.get(job_id)
        if not job:
            json_response(self, 404, {"ok": False, "error": "not found"})
            return
        json_response(self, 200, {"ok": True, "job": job})

    def handle_history(self):
        ip = self._client_ip()
        items = read_history()
        filtered = [i for i in items if i.get("client_ip", "") in ("", ip)]
        json_response(self, 200, {"ok": True, "history": filtered[-100:]})

    def handle_cache_clean(self):
        removed_uploads = 0
        if UPLOAD_DIR.exists():
            for f in UPLOAD_DIR.iterdir():
                if f.is_file():
                    f.unlink()
                    removed_uploads += 1
        json_response(self, 200, {"ok": True, "removed_uploads": removed_uploads})

    def handle_preset_save(self):
        content_type = self.headers.get("Content-Type", "")
        if "multipart" in content_type:
            fields, files = parse_multipart(self)
        else:
            fields = read_json_body(self)
            files = {}

        values = {}
        for k, v in fields.items():
            if k not in ("archive_name",):
                values[k] = v

        media_info = {}
        for key, (filename, blob) in files.items():
            suffix = Path(filename).suffix or ".bin"
            stored_name = f"{key}{suffix}"
            (MEDIA_DIR / stored_name).write_bytes(blob)
            media_info[key] = {"filename": filename, "stored": stored_name, "mime": mimetypes.guess_type(filename)[0] or "application/octet-stream"}

        preset = read_preset()
        preset["values"] = values
        preset["media"].update(media_info)
        write_preset(preset)

        archive_name = fields.get("archive_name", "").strip()
        if archive_name:
            save_archive(archive_name, preset)

        json_response(self, 200, {"ok": True, **preset_for_client()})

    def handle_preset_clear(self):
        if MEDIA_DIR.exists():
            shutil.rmtree(MEDIA_DIR)
        MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        write_preset({"values": {}, "media": {}})
        json_response(self, 200, {"ok": True})

    def handle_preset_media(self, field: str):
        preset = read_preset()
        item = preset.get("media", {}).get(field)
        if not item:
            self.send_error(404)
            return
        path = MEDIA_DIR / item["stored"]
        if not path.exists():
            self.send_error(404)
            return
        mime = item.get("mime", mimetypes.guess_type(str(path))[0] or "application/octet-stream")
        content = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Content-Disposition", f'inline; filename="{item.get("filename", path.name)}"')
        self.end_headers()
        self.wfile.write(content)

    def handle_archive_load(self):
        data = read_json_body(self)
        name = data.get("name", "")
        if not name:
            json_response(self, 400, {"ok": False, "error": "name required"})
            return
        result = load_archive(name)
        if result is None:
            json_response(self, 404, {"ok": False, "error": "archive not found"})
            return
        json_response(self, 200, {"ok": True, **result})

    def handle_archive_delete(self):
        data = read_json_body(self)
        name = data.get("name", "")
        if not name:
            json_response(self, 400, {"ok": False, "error": "name required"})
            return
        if delete_archive(name):
            json_response(self, 200, {"ok": True})
        else:
            json_response(self, 404, {"ok": False, "error": "archive not found"})

    def handle_archive_from_history(self):
        data = read_json_body(self)
        job_id = data.get("job_id", "")
        archive_name = data.get("archive_name", "").strip()
        if not job_id or not archive_name:
            json_response(self, 400, {"ok": False, "error": "job_id and archive_name required"})
            return

        items = read_history()
        target = None
        for item in items:
            if item.get("job_id") == job_id:
                target = item
                break
        if not target:
            json_response(self, 404, {"ok": False, "error": "job not found in history"})
            return

        params = target.get("params", {})
        media_info = {}
        for key in sorted(params.keys()):
            if key.startswith(("ref_image_", "ref_video_", "ref_audio_", "frame_", "first_frame", "last_frame")):
                path = Path(params[key]) if params[key] else None
                if path and path.exists():
                    suffix = path.suffix or ".bin"
                    stored_name = f"{key}{suffix}"
                    shutil.copy2(path, MEDIA_DIR / stored_name)
                    media_info[key] = {"filename": path.name, "stored": stored_name, "mime": mimetypes.guess_type(str(path))[0] or "application/octet-stream"}

        values = {k: v for k, v in params.items() if not k.startswith(("ref_image_", "ref_video_", "ref_audio_", "frame_", "first_frame", "last_frame"))}
        preset = {"values": values, "media": media_info}
        save_archive(archive_name, preset)
        json_response(self, 200, {"ok": True, "archive_name": sanitize_archive_name(archive_name), "archives": list_archives()})

    def handle_meta(self):
        cfg = load_config()
        json_response(self, 200, {
            "app": "dreamina",
            "version": APP_VERSION,
            "capabilities": ["text2image", "image2image", "frames2video", "multimodal2video", "multiframe2video"],
            "max_concurrent": cfg["max_concurrent"],
            "status": "ready",
        })

    def serve_static(self, path: str):
        if path == "/" or path == "":
            path = "/index.html"
        file_path = STATIC_DIR / path.lstrip("/")
        if not file_path.exists() or not file_path.is_file():
            file_path = STATIC_DIR / "index.html"
        if not file_path.exists():
            self.send_error(404)
            return
        mime = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        content = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def serve_file(self, base_dir: Path, rel_path: str):
        file_path = base_dir / urllib.parse.unquote(rel_path)
        if not file_path.exists() or not file_path.is_file():
            self.send_error(404)
            return
        mime = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        content = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)


def main():
    global EXECUTOR
    ensure_dirs()
    cleanup_old_uploads()

    cfg = load_config()
    EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=cfg["max_concurrent"])

    port = find_available_port(cfg["port"])
    host = cfg.get("host", "127.0.0.1")
    server = ThreadingHTTPServer((host, port), Handler)

    url = f"http://127.0.0.1:{port}"
    print(f"Dreamina App running at {url}")
    print("Press Ctrl+C to stop")

    if not os.environ.get("CORS"):
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()
        EXECUTOR.shutdown(wait=False)


if __name__ == "__main__":
    main()
