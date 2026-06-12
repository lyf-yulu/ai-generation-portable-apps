#!/usr/bin/env python3
from __future__ import annotations

import concurrent.futures
import json
import mimetypes
import os
import re
import shutil
import signal
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

# Windows: suppress console windows for spawned subprocesses
_POPEN_EXTRA: dict[str, Any] = {}
if hasattr(subprocess, "CREATE_NO_WINDOW"):
    _POPEN_EXTRA["creationflags"] = subprocess.CREATE_NO_WINDOW

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
OUTPUT_DIR = ROOT / "outputs"
UPLOAD_DIR = ROOT / "uploads"
LOG_DIR = ROOT / "logs"
STATE_DIR = ROOT / "state"
ARCHIVE_DIR = ROOT / "archives"
ACCOUNTS_DIR = ROOT / "accounts"
MEDIA_DIR = STATE_DIR / "media"
PRESET_PATH = STATE_DIR / "preset.json"
HISTORY_PATH = STATE_DIR / "history.json"
ACCOUNTS_PATH = STATE_DIR / "accounts.json"
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
    for d in (OUTPUT_DIR, UPLOAD_DIR, LOG_DIR, STATE_DIR, ARCHIVE_DIR, MEDIA_DIR, ACCOUNTS_DIR):
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


def run_cmd(args: list[str], timeout: int = 30, env_override: dict | None = None) -> dict[str, Any]:
    try:
        env = None
        if env_override:
            env = os.environ.copy()
            env.update(env_override)
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, env=env
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


# === Accounts Module ===

ROUND_ROBIN_INDEX = 0

def load_accounts() -> dict[str, Any]:
    if ACCOUNTS_PATH.exists():
        try:
            data = json.loads(ACCOUNTS_PATH.read_text("utf-8"))
            if isinstance(data.get("accounts"), list):
                # Auto-recovery: if accounts.json is empty but account dirs exist on disk,
                # scan them back — prevent silent data loss from corrupted saves.
                if not data["accounts"] and ACCOUNTS_DIR.exists():
                    disk_ids = [d.name for d in ACCOUNTS_DIR.iterdir()
                                if d.is_dir() and d.name.startswith("acc_")]
                    if disk_ids:
                        recovered = _rebuild_accounts_from_disk(disk_ids)
                        if recovered:
                            return recovered
                return data
        except Exception:
            pass
    # If file doesn't exist but account dirs do, rebuild
    if ACCOUNTS_DIR.exists():
        disk_ids = [d.name for d in ACCOUNTS_DIR.iterdir()
                    if d.is_dir() and d.name.startswith("acc_")]
        if disk_ids:
            recovered = _rebuild_accounts_from_disk(disk_ids)
            if recovered:
                return recovered
    return {"accounts": [], "active_account": None, "dispatch_mode": "manual"}


def _rebuild_accounts_from_disk(account_ids: list[str]) -> dict[str, Any] | None:
    """Scan account directories and rebuild accounts.json after data loss."""
    accounts = []
    for acc_id in sorted(account_ids):
        home = get_account_home(acc_id)
        cli_dir = home / ".dreamina_cli"
        has_session = cli_dir.exists()
        accounts.append({
            "id": acc_id,
            "name": f"账号{len(accounts) + 1}",
            "uid": None,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "home_dir": str(home),
            "is_system_home": False,
            "logged_in": has_session,
            "credit": None,
            "_login_verified_at": time.time() if has_session else 0,
        })
    if not accounts:
        return None
    data = {
        "accounts": accounts,
        "active_account": accounts[0]["id"],
        "dispatch_mode": "manual",
    }
    # Persist the recovered data so next load is fast
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = ACCOUNTS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(ACCOUNTS_PATH)
    return data


def save_accounts(data: dict[str, Any]):
    # Defensive: refuse to overwrite existing non-empty accounts with an empty list.
    # This guards against buggy code paths that might pass a fresh empty dict.
    if isinstance(data.get("accounts"), list) and not data["accounts"]:
        if ACCOUNTS_PATH.exists():
            try:
                existing = json.loads(ACCOUNTS_PATH.read_text("utf-8"))
                if isinstance(existing.get("accounts"), list) and existing["accounts"]:
                    print(f"  [WARN] save_accounts refused to overwrite {len(existing['accounts'])} accounts with empty list")
                    return
            except Exception:
                pass
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    # Write to temp file + atomic rename
    tmp = ACCOUNTS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(ACCOUNTS_PATH)


def get_account_home(account_id: str) -> Path:
    return ACCOUNTS_DIR / account_id


def ensure_account_home(account_id: str) -> Path:
    home = get_account_home(account_id)
    home.mkdir(parents=True, exist_ok=True)
    # macOS: create isolated keychain so dreamina login creds don't conflict across accounts
    if sys.platform == "darwin":
        keychains_dir = home / "Library" / "Keychains"
        keychain_file = keychains_dir / "login.keychain-db"
        if not keychain_file.exists():
            keychains_dir.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["security", "create-keychain", "-p", "", str(keychain_file)],
                capture_output=True, timeout=10
            )
            subprocess.run(
                ["security", "unlock-keychain", "-p", "", str(keychain_file)],
                capture_output=True, timeout=10
            )
            subprocess.run(
                ["security", "set-keychain-settings", str(keychain_file)],
                capture_output=True, timeout=10
            )
            r = subprocess.run(
                ["security", "list-keychains", "-d", "user"],
                capture_output=True, text=True, timeout=10
            )
            existing = [l.strip().strip('"') for l in r.stdout.splitlines() if l.strip()]
            existing.append(str(keychain_file))
            subprocess.run(
                ["security", "list-keychains", "-d", "user", "-s"] + existing,
                capture_output=True, timeout=10
            )
    # Windows/Linux: dreamina stores session in $HOME/.dreamina_cli,
    # get_account_env() sets HOME per-account for isolation
    return home


def get_account_env(account_id: str) -> dict[str, str] | None:
    acc = get_account_by_id(account_id)
    if acc and acc.get("is_system_home"):
        return None
    home = ensure_account_home(account_id)
    return {"HOME": str(home)}


def get_account_by_id(account_id: str) -> dict[str, Any] | None:
    data = load_accounts()
    for acc in data["accounts"]:
        if acc["id"] == account_id:
            return acc
    return None


def check_account_login(account_id: str) -> dict[str, Any]:
    env = get_account_env(account_id)
    return check_login_with_env(env)


def check_login_with_env(env: dict | None = None) -> dict[str, Any]:
    r = run_cmd(["dreamina", "user_credit"], timeout=15, env_override=env)
    if r["returncode"] != 0:
        return {"logged_in": False, "credit": None, "raw": r["stderr"]}
    try:
        data = json.loads(r["stdout"])
        return {"logged_in": True, "credit": data}
    except json.JSONDecodeError:
        if "credit" in r["stdout"].lower() or "{" in r["stdout"]:
            return {"logged_in": True, "credit": r["stdout"]}
        return {"logged_in": False, "credit": None, "raw": r["stdout"]}


def pick_account_for_job() -> dict[str, Any] | None:
    global ROUND_ROBIN_INDEX
    data = load_accounts()
    accounts = data["accounts"]

    # Only trust login status verified within the last 30 minutes.
    # Missing _login_verified_at (legacy accounts) is treated as "recently verified"
    # to maintain backward compatibility.
    now = time.time()
    max_staleness = 1800  # 30 minutes
    logged_in = [
        a for a in accounts
        if a.get("logged_in")
    ]
    # Filter out accounts whose login status is too stale (skip if field missing)
    logged_in = [
        a for a in logged_in
        if "_login_verified_at" not in a or (now - a["_login_verified_at"]) < max_staleness
    ]
    if not logged_in:
        return None
    mode = data.get("dispatch_mode", "manual")
    if mode == "manual":
        active_id = data.get("active_account")
        for a in logged_in:
            if a["id"] == active_id:
                return a
        return logged_in[0]
    elif mode == "round_robin":
        idx = ROUND_ROBIN_INDEX % len(logged_in)
        ROUND_ROBIN_INDEX += 1
        return logged_in[idx]
    return logged_in[0]


def migrate_default_account():
    """First-run migration: if no accounts exist but ~/.dreamina_cli is logged in, create default account."""
    data = load_accounts()
    if data["accounts"]:
        return
    home = Path.home()
    cli_dir = home / ".dreamina_cli"
    if not cli_dir.exists():
        return
    status = check_login()
    if not status.get("logged_in"):
        return
    acc_id = "acc_default"
    acc = {
        "id": acc_id,
        "name": "默认账号",
        "uid": None,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "home_dir": str(home),
        "is_system_home": True,
        "logged_in": True,
        "credit": status.get("credit"),
        "_login_verified_at": time.time(),
    }
    data["accounts"].append(acc)
    data["active_account"] = acc_id
    save_accounts(data)


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


def parse_cli_json(stdout: str) -> dict[str, Any]:
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        pass
    idx = stdout.find("\n{")
    if idx >= 0:
        try:
            return json.loads(stdout[idx + 1:])
        except json.JSONDecodeError:
            pass
    idx = stdout.rfind("{")
    if idx >= 0:
        try:
            return json.loads(stdout[idx:])
        except json.JSONDecodeError:
            pass
    return {"raw": stdout}


def execute_task(job_id: str, task_type: str, args: list[str], params: dict[str, Any]):
    with LOCK:
        job = JOBS[job_id]
        job["status"] = "running"

    total = job.get("total", 1)
    concurrency = job.get("concurrency", 1)
    env_override = params.get("env_override")

    def add_event(msg: str):
        with LOCK:
            job["events"].append({"time": time.strftime("%H:%M:%S"), "message": msg})

    def add_cli_log(cmd_args, result):
        with LOCK:
            if "cli_logs" not in job:
                job["cli_logs"] = []
            job["cli_logs"].append({
                "time": time.strftime("%H:%M:%S"),
                "command": " ".join(cmd_args),
                "returncode": result["returncode"],
                "stdout": result["stdout"][:2000],
                "stderr": result["stderr"][:500],
            })

    def run_one(index: int):
        add_event(f"子任务 {index}/{total} 开始")
        max_retries = 10
        retry_interval = 30
        for attempt in range(max_retries):
            result = run_cmd(args, timeout=params.get("timeout", 600), env_override=env_override)
            add_cli_log(args, result)
            stdout_text = result.get("stdout", "") + result.get("stderr", "")
            if "ExceedConcurrencyLimit" in stdout_text or "ret=1310" in stdout_text:
                add_event(f"子任务 {index}/{total} 并发限制，{retry_interval}秒后重试 ({attempt+1}/{max_retries})")
                time.sleep(retry_interval)
                continue
            break
        with LOCK:
            job["done"] += 1
        if result["returncode"] == 0:
            data = parse_cli_json(result["stdout"])
            if data.get("gen_status") == "fail":
                reason = data.get("fail_reason") or "generation failed"
                with LOCK:
                    job["errors"].append(f"[{index}] {reason}")
                add_event(f"子任务 {index}/{total} 失败: {reason[:80]}")
                return
            submit_id = data.get("submit_id") or ""
            if submit_id:
                dl = download_if_needed(submit_id, data, task_type, job_id,
                                        output_name=job.get("output_name", ""),
                                        output_dir=job.get("output_dir", ""),
                                        sub_index=index, total=total,
                                        env_override=env_override)
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
        "cli_logs": final_job.get("cli_logs", []),
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


def download_if_needed(submit_id: str, data: dict, task_type: str, job_id: str, output_name: str = "", output_dir: str = "", sub_index: int = 1, total: int = 1, env_override: dict | None = None) -> dict | None:
    if not submit_id:
        return None
    base_dir = resolve_output_dir(output_dir) if output_dir else OUTPUT_DIR
    ts = time.strftime("%Y%m%d_%H%M%S")
    short_id = job_id[:8]
    custom_name = (output_name or "").strip()
    if custom_name:
        if total > 1:
            dl_dir = base_dir / f"{custom_name}-{sub_index}"
        else:
            dl_dir = base_dir / custom_name
        if dl_dir.exists():
            dl_dir = base_dir / f"{custom_name}-{sub_index}_{ts}"
    else:
        dl_dir = base_dir / f"{ts}_{task_type}_{short_id}"
    dl_dir.mkdir(parents=True, exist_ok=True)

    cfg = load_config()
    poll_timeout = cfg["poll_video"] if "video" in task_type else cfg["poll_image"]
    deadline = time.time() + poll_timeout
    interval = 10

    while True:
        r = run_cmd(["dreamina", "query_result", f"--submit_id={submit_id}",
                     f"--download_dir={dl_dir}"], timeout=60, env_override=env_override)
        if r["returncode"] != 0:
            return {"download_dir": str(dl_dir.relative_to(ROOT)), "files": [],
                    "error": r["stderr"] or "query_result failed"}

        result_data = parse_cli_json(r["stdout"])
        gs = result_data.get("gen_status", "")

        if gs == "fail":
            reason = result_data.get("fail_reason", "generation failed")
            return {"download_dir": str(dl_dir.relative_to(ROOT)), "files": [],
                    "error": reason, "gen_status": "fail"}

        if gs != "querying":
            files = [str(f.relative_to(ROOT)) for f in dl_dir.iterdir() if f.is_file()]
            return {"download_dir": str(dl_dir.relative_to(ROOT)), "files": files,
                    "cli_output": r["stdout"], "gen_status": gs}

        # Report queue progress to job events
        qi = result_data.get("queue_info", {})
        with LOCK:
            job = JOBS.get(job_id)
            if job:
                queue_msg = f"排队中"
                if qi.get("queue_idx"):
                    queue_msg += f" (第{qi['queue_idx']}位/共{qi.get('queue_length', '?')})"
                job["events"].append({"time": time.strftime("%H:%M:%S"), "message": queue_msg})

        if time.time() >= deadline:
            return {"download_dir": str(dl_dir.relative_to(ROOT)), "files": [],
                    "error": "poll timeout", "gen_status": "querying",
                    "queue_info": qi}

        time.sleep(interval)


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


def preset_for_client(handler: SimpleHTTPRequestHandler | None = None) -> dict[str, Any]:
    data = read_preset()
    media = {}
    for field, item in data.get("media", {}).items():
        path = MEDIA_DIR / item.get("stored", "")
        if path.exists():
            media[field] = {
                "filename": item.get("filename", path.name),
                "url": f"/api/preset-media/{field}",
            }
    return {"values": data.get("values", {}), "media": media, "archives": list_archives(handler)}


def list_archives(handler: SimpleHTTPRequestHandler | None = None) -> list[dict[str, Any]]:
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    dir_path = _archive_dir_for(handler) if handler else ARCHIVE_DIR
    dir_path.mkdir(parents=True, exist_ok=True)
    archives = []
    for f in sorted(dir_path.iterdir()):
        if f.suffix == ".dreamina" and f.is_file():
            archives.append({"name": f.stem, "size": f.stat().st_size, "mtime": f.stat().st_mtime})
    return archives


def save_archive(name: str, preset: dict[str, Any], handler: SimpleHTTPRequestHandler | None = None):
    safe_name = sanitize_archive_name(name)
    dir_path = _archive_dir_for(handler) if handler else ARCHIVE_DIR
    dir_path.mkdir(parents=True, exist_ok=True)
    path = dir_path / f"{safe_name}.dreamina"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("preset.json", json.dumps(preset, ensure_ascii=False, indent=2))
        for field, item in preset.get("media", {}).items():
            src = MEDIA_DIR / item.get("stored", "")
            if src.exists():
                zf.write(src, f"media/{item['stored']}")
    return safe_name


def load_archive(name: str, handler: SimpleHTTPRequestHandler | None = None) -> dict[str, Any] | None:
    dir_path = _archive_dir_for(handler) if handler else ARCHIVE_DIR
    path = dir_path / f"{name}.dreamina"
    if not path.exists():
        # Fallback to legacy top-level location + migrate
        legacy = ARCHIVE_DIR / f"{name}.dreamina"
        if legacy.exists():
            path = legacy
        else:
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
    # Migrate legacy to IP-scoped
    if handler is not None and path.parent != dir_path:
        save_archive(name, preset, handler)
    return preset_for_client(handler)


def delete_archive(name: str, handler: SimpleHTTPRequestHandler | None = None) -> bool:
    dir_path = _archive_dir_for(handler) if handler else ARCHIVE_DIR
    path = dir_path / f"{name}.dreamina"
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
        elif path == "/api/accounts":
            self.handle_accounts_list()
        elif path.startswith("/api/accounts/") and path.endswith("/login-poll"):
            acc_id = path.split("/api/accounts/")[1].split("/")[0]
            self.handle_account_login_poll(acc_id)
        elif path == "/api/jobs":
            self.handle_jobs_list()
        elif path.startswith("/api/jobs/"):
            job_id = path.split("/api/jobs/")[1].split("/")[0]
            self.handle_job_status(job_id)
        elif path == "/api/history":
            self.handle_history()
        elif path == "/api/preset":
            json_response(self, 200, {"ok": True, **preset_for_client(self)})
        elif path == "/api/archives":
            json_response(self, 200, {"ok": True, "archives": list_archives(self)})
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
        elif path == "/api/accounts":
            self.handle_account_create()
        elif path.startswith("/api/accounts/") and path.endswith("/login"):
            acc_id = path.split("/api/accounts/")[1].split("/")[0]
            self.handle_account_login(acc_id)
        elif path.startswith("/api/accounts/") and path.endswith("/logout"):
            acc_id = path.split("/api/accounts/")[1].split("/")[0]
            self.handle_account_logout(acc_id)
        elif path.startswith("/api/accounts/") and path.endswith("/refresh"):
            acc_id = path.split("/api/accounts/")[1].split("/")[0]
            self.handle_account_refresh(acc_id)
        elif path.startswith("/api/accounts/") and path.endswith("/delete"):
            acc_id = path.split("/api/accounts/")[1].split("/")[0]
            self.handle_account_delete(acc_id)
        elif path == "/api/accounts/active":
            self.handle_set_active_account()
        elif path == "/api/dispatch-mode":
            self.handle_set_dispatch_mode()
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
            client_ip = self.headers.get("X-Forwarded-For") or self.client_address[0]
            if client_ip not in ("127.0.0.1", "::1", "localhost"):
                json_response(self, 200, {"remote": True})
                return
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
        accounts_data = load_accounts()
        json_response(self, 200, {
            "ok": True,
            "cli_installed": installed,
            "logged_in": login_info["logged_in"],
            "credit": login_info.get("credit"),
            "accounts": accounts_data,
        })

    def handle_login_poll(self):
        info = check_login()
        json_response(self, 200, {"ok": True, **info})

    # === Account Management Handlers ===

    def handle_accounts_list(self):
        data = load_accounts()
        json_response(self, 200, {"ok": True, **data})

    def handle_account_create(self):
        body = read_json_body(self)
        name = body.get("name", "").strip() or f"账号{len(load_accounts()['accounts']) + 1}"
        acc_id = f"acc_{uuid.uuid4().hex[:8]}"
        acc = {
            "id": acc_id,
            "name": name,
            "uid": None,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "home_dir": str(get_account_home(acc_id)),
            "is_system_home": False,
            "logged_in": False,
            "credit": None,
        }
        ensure_account_home(acc_id)
        data = load_accounts()
        data["accounts"].append(acc)
        if not data["active_account"]:
            data["active_account"] = acc_id
        save_accounts(data)
        json_response(self, 200, {"ok": True, "account": acc})

    def handle_account_login(self, acc_id: str):
        acc = get_account_by_id(acc_id)
        if not acc:
            json_response(self, 404, {"ok": False, "error": "account not found"})
            return
        env = get_account_env(acc_id)
        global LOGIN_PROC
        with LOGIN_LOCK:
            if LOGIN_PROC and LOGIN_PROC.poll() is None:
                json_response(self, 200, {"ok": True, "message": "login already in progress"})
                return
            try:
                proc_env = os.environ.copy()
                if env:
                    proc_env.update(env)
                LOGIN_PROC = subprocess.Popen(
                    ["dreamina", "login"],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, start_new_session=True, env=proc_env,
                    **_POPEN_EXTRA,
                )
            except FileNotFoundError:
                json_response(self, 400, {"ok": False, "error": "dreamina not found"})
                return

        cfg = load_config()
        timeout = cfg.get("login_timeout", 120)
        auth_url = ""

        def read_output():
            nonlocal auth_url
            try:
                for line in LOGIN_PROC.stdout:
                    if "verification_uri:" in line:
                        auth_url = line.split("verification_uri:", 1)[1].strip()
                        break
            except Exception:
                pass

        threading.Thread(target=read_output, daemon=True).start()

        def kill_after_timeout():
            time.sleep(timeout)
            with LOGIN_LOCK:
                if LOGIN_PROC and LOGIN_PROC.poll() is None:
                    LOGIN_PROC.kill()

        threading.Thread(target=kill_after_timeout, daemon=True).start()

        time.sleep(2)
        json_response(self, 200, {"ok": True, "message": "login started", "account_id": acc_id, "timeout": timeout, "auth_url": auth_url})

    def handle_account_login_poll(self, acc_id: str):
        acc = get_account_by_id(acc_id)
        if not acc:
            json_response(self, 404, {"ok": False, "error": "account not found"})
            return
        env = get_account_env(acc_id)
        info = check_login_with_env(env)
        if info["logged_in"]:
            data = load_accounts()
            for a in data["accounts"]:
                if a["id"] == acc_id:
                    a["logged_in"] = True
                    a["credit"] = info.get("credit")
                    a["_login_verified_at"] = time.time()
                    break
            save_accounts(data)
        json_response(self, 200, {"ok": True, "account_id": acc_id, **info})

    def handle_account_logout(self, acc_id: str):
        acc = get_account_by_id(acc_id)
        if not acc:
            json_response(self, 404, {"ok": False, "error": "account not found"})
            return
        env = get_account_env(acc_id)
        run_cmd(["dreamina", "logout"], timeout=10, env_override=env)
        data = load_accounts()
        for a in data["accounts"]:
            if a["id"] == acc_id:
                a["logged_in"] = False
                a["credit"] = None
                break
        save_accounts(data)
        json_response(self, 200, {"ok": True, "message": "logged out"})

    def handle_account_refresh(self, acc_id: str):
        acc = get_account_by_id(acc_id)
        if not acc:
            json_response(self, 404, {"ok": False, "error": "account not found"})
            return
        env = get_account_env(acc_id)
        info = check_login_with_env(env)
        data = load_accounts()
        for a in data["accounts"]:
            if a["id"] == acc_id:
                a["logged_in"] = info["logged_in"]
                a["credit"] = info.get("credit")
                a["_login_verified_at"] = time.time()
                break
        save_accounts(data)
        json_response(self, 200, {"ok": True, "account_id": acc_id, **info})

    def handle_account_delete(self, acc_id: str):
        acc = get_account_by_id(acc_id)
        if not acc:
            json_response(self, 404, {"ok": False, "error": "account not found"})
            return
        if acc.get("is_system_home"):
            json_response(self, 400, {"ok": False, "error": "cannot delete system home account"})
            return
        home = get_account_home(acc_id)
        if home.exists():
            shutil.rmtree(home, ignore_errors=True)
        data = load_accounts()
        data["accounts"] = [a for a in data["accounts"] if a["id"] != acc_id]
        if data["active_account"] == acc_id:
            data["active_account"] = data["accounts"][0]["id"] if data["accounts"] else None
        save_accounts(data)
        json_response(self, 200, {"ok": True, "message": "account deleted"})

    def handle_set_active_account(self):
        body = read_json_body(self)
        acc_id = body.get("account_id", "")
        if not get_account_by_id(acc_id):
            json_response(self, 404, {"ok": False, "error": "account not found"})
            return
        data = load_accounts()
        data["active_account"] = acc_id
        save_accounts(data)
        json_response(self, 200, {"ok": True, "active_account": acc_id})

    def handle_set_dispatch_mode(self):
        body = read_json_body(self)
        mode = body.get("mode", "manual")
        if mode not in ("manual", "round_robin"):
            json_response(self, 400, {"ok": False, "error": "invalid mode"})
            return
        data = load_accounts()
        data["dispatch_mode"] = mode
        save_accounts(data)
        json_response(self, 200, {"ok": True, "dispatch_mode": mode})

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
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                **_POPEN_EXTRA,
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
                    **_POPEN_EXTRA,
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
                    **_POPEN_EXTRA,
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
        if not prompt and task_type not in ("multimodal2video", "multiframe2video"):
            json_response(self, 400, {"ok": False, "error": "prompt is required"})
            return

        uploaded_paths = {}
        if files:
            uploaded_paths["ref_image"] = save_uploaded_files(files, "ref_image_") + save_uploaded_files(files, "mm_image_")
            uploaded_paths["ref_video"] = save_uploaded_files(files, "ref_video_") + save_uploaded_files(files, "mm_video_")
            uploaded_paths["ref_audio"] = save_uploaded_files(files, "ref_audio_") + save_uploaded_files(files, "mm_audio_")
            uploaded_paths["first_frame"] = save_uploaded_files(files, "first_frame")
            uploaded_paths["last_frame"] = save_uploaded_files(files, "last_frame")
            uploaded_paths["frame_"] = save_uploaded_files(files, "frame_")
            if not any(uploaded_paths.values()):
                legacy = save_uploaded_files(files, "image")
                if legacy:
                    uploaded_paths["ref_image"] = legacy

        args = self.build_cli_args(task_type, fields, uploaded_paths, cfg)
        # Use poll timeout from config + 120s buffer for CLI command itself
        is_video = "video" in task_type or "frame" in task_type
        cli_timeout = max(120, cfg.get("poll_video" if is_video else "poll_image", 300))

        repeat_count = max(1, min(10, int(fields.get("repeat_count") or 1)))
        concurrency_val = 1  # Dreamina enforces per-account concurrency=1
        total = repeat_count

        # Store uploaded file paths as relative strings so retry can rebuild them
        uploaded_paths_rel = {}
        for k, paths in uploaded_paths.items():
            if paths:
                uploaded_paths_rel[k] = [str(p.relative_to(ROOT)) for p in paths]

        job_id = uuid.uuid4().hex
        job = {
            "job_id": job_id,
            "task_type": task_type,
            "status": "pending",
            "total": total,
            "done": 0,
            "concurrency": concurrency_val,
            "output_name": fields.get("output_name", ""),
            "output_dir": fields.get("output_dir", ""),
            "client_ip": self._client_ip(),
            "events": [],
            "results": [],
            "errors": [],
            "params": {k: v for k, v in fields.items()},
            "uploaded_paths": uploaded_paths_rel,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "finished_at": None,
            "result": None,
            "error": None,
            "retryable": False,
        }

        with LOCK:
            JOBS[job_id] = job

        record_job(job)

        account = pick_account_for_job()
        env_override = get_account_env(account["id"]) if account else None
        job["account_id"] = account["id"] if account else None

        EXECUTOR.submit(execute_task, job_id, task_type, args, {"timeout": cli_timeout, "env_override": env_override})

        json_response(self, 200, {"ok": True, "job_id": job_id, "account_id": job.get("account_id")})

    def build_cli_args(self, task_type: str, fields: dict, uploaded_paths: dict, cfg: dict) -> list[str]:
        prompt = fields.get("prompt", "")
        ratio = fields.get("ratio", "1:1")
        resolution = fields.get("resolution_type", "2k")
        duration = fields.get("duration", "5")
        video_resolution = fields.get("video_resolution", "720p").lower()
        model_version = fields.get("model_version", "seedance2.0fast_vip")

        if task_type == "text2image":
            return ["dreamina", "text2image", f"--prompt={prompt}", f"--ratio={ratio}", f"--resolution_type={resolution}", "--poll=0"]

        elif task_type == "image2image":
            images = uploaded_paths.get("ref_image", [])
            img_str = ",".join(str(p) for p in images) if images else ""
            return ["dreamina", "image2image", "--images", img_str, f"--prompt={prompt}", f"--ratio={ratio}", f"--resolution_type={resolution}", "--poll=0"]

        elif task_type == "text2video":
            return ["dreamina", "text2video", f"--prompt={prompt}", f"--duration={duration}", f"--ratio={ratio}", f"--video_resolution={video_resolution}", f"--model_version={model_version}", "--poll=0"]

        elif task_type == "image2video":
            images = uploaded_paths.get("ref_image", [])
            img = str(images[0]) if images else ""
            return ["dreamina", "image2video", "--image", img, f"--prompt={prompt}", f"--duration={duration}", f"--video_resolution={video_resolution}", f"--model_version={model_version}", "--poll=0"]

        elif task_type == "frames2video":
            first_list = uploaded_paths.get("first_frame", [])
            last_list = uploaded_paths.get("last_frame", [])
            first = str(first_list[0]) if first_list else ""
            last = str(last_list[0]) if last_list else ""
            args = ["dreamina", "frames2video", "--first", first, "--last", last, f"--prompt={prompt}", f"--duration={duration}", f"--video_resolution={video_resolution}", "--poll=0"]
            if model_version:
                args.append(f"--model_version={model_version}")
            return args

        elif task_type == "multimodal2video":
            args = ["dreamina", "multimodal2video", f"--duration={duration}", f"--ratio={ratio}", f"--video_resolution={video_resolution}", f"--model_version={model_version}", "--poll=0"]
            if prompt:
                args.append(f"--prompt={prompt}")
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
            args = ["dreamina", "multiframe2video", "--images", img_str, "--poll=0"]
            if prompt:
                args.extend(["--prompt", prompt])
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

        # Rebuild uploaded_paths from stored relative paths; drop files that no longer exist
        uploaded_paths: dict[str, list[Path]] = {}
        missing_files: list[str] = []
        saved = job.get("uploaded_paths") or {}
        for key, rel_paths in saved.items():
            resolved = []
            for rel in rel_paths:
                p = ROOT / rel
                if p.is_file():
                    resolved.append(p)
                else:
                    missing_files.append(rel)
            if resolved:
                uploaded_paths[key] = resolved

        # Build CLI args with available files; build_cli_args handles empty lists gracefully
        args = self.build_cli_args(task_type, fields, uploaded_paths, cfg)

        is_video = "video" in task_type or "frame" in task_type
        poll_timeout_s = cfg["poll_video"] if is_video else cfg["poll_image"]
        cli_timeout = max(120, poll_timeout_s)

        repeat_count = max(1, min(10, int(fields.get("repeat_count") or 1)))
        concurrency_val = 1
        total = repeat_count

        # Convert resolved upload paths back to relative for the retry job record
        uploaded_paths_rel = {}
        for k, paths in uploaded_paths.items():
            if paths:
                uploaded_paths_rel[k] = [str(p.relative_to(ROOT)) for p in paths]

        new_job_id = uuid.uuid4().hex
        new_job = {
            "job_id": new_job_id,
            "task_type": task_type,
            "status": "pending",
            "total": total,
            "done": 0,
            "concurrency": concurrency_val,
            "output_name": fields.get("output_name", ""),
            "output_dir": fields.get("output_dir", ""),
            "client_ip": job.get("client_ip", ""),
            "events": [],
            "results": [],
            "errors": [],
            "params": fields,
            "uploaded_paths": uploaded_paths_rel,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "finished_at": None,
            "result": None,
            "error": None,
            "retryable": False,
        }
        with LOCK:
            JOBS[new_job_id] = new_job
        record_job(new_job)

        account = pick_account_for_job()
        env_override = get_account_env(account["id"]) if account else None
        new_job["account_id"] = account["id"] if account else None

        # Notify user when referenced files are gone (text-only modes still work)
        if missing_files:
            with LOCK:
                new_job["events"].append({
                    "time": time.strftime("%H:%M:%S"),
                    "message": f"⚠ 部分素材已过期/丢失 ({len(missing_files)} 个)，仅使用可用素材重试",
                })

        EXECUTOR.submit(execute_task, new_job_id, task_type, args,
                        {"timeout": cli_timeout, "env_override": env_override})
        json_response(self, 200, {"ok": True, "job_id": new_job_id,
                                   "missing_files": len(missing_files) if missing_files else 0})

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
            save_archive(archive_name, preset, self)

        json_response(self, 200, {"ok": True, **preset_for_client(self)})

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
        result = load_archive(name, self)
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
        if delete_archive(name, self):
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
        save_archive(archive_name, preset, self)
        json_response(self, 200, {"ok": True, "archive_name": sanitize_archive_name(archive_name), "archives": list_archives(self)})

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
    migrate_default_account()

    cfg = load_config()
    EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=cfg["max_concurrent"])

    port = int(os.environ.get("PORT", 0)) or find_available_port(cfg["port"])
    host = os.environ.get("HOST") or cfg.get("host", "127.0.0.1")
    server = ThreadingHTTPServer((host, port), Handler)

    url = f"http://127.0.0.1:{port}"
    print(f"Dreamina App running at {url}")
    print("Press Ctrl+C to stop")

    if not os.environ.get("CORS"):
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    def shutdown_handler(*args):
        print("\nShutting down...")
        server.shutdown()
        EXECUTOR.shutdown(wait=False)
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
        EXECUTOR.shutdown(wait=False)


if __name__ == "__main__":
    main()
