#!/usr/bin/env python3
from __future__ import annotations

import csv
from datetime import datetime, timedelta
import hashlib
import http.client
import io
import json
import mimetypes
import os
import re
import secrets
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
import uuid
import urllib.parse
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
_DATA_BASE = Path(os.environ.get("DATA_DIR", str(ROOT)))
STATIC_DIR = ROOT / "static"

# Ensure `portal/` dir is on sys.path so `import daily_report` works whether
# the process was launched from repo root or from portal/.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import daily_report as _daily_report_module

_POPEN_EXTRA: dict[str, Any] = {}
if hasattr(subprocess, "CREATE_NO_WINDOW"):
    _POPEN_EXTRA["creationflags"] = subprocess.CREATE_NO_WINDOW

STATE_DIR = _DATA_BASE / "state"
USAGE_PATH = STATE_DIR / "usage.json"
USAGE_JSONL_RETENTION_DAYS = 30
USERS_PATH = STATE_DIR / "users.json"
SESSIONS_PATH = STATE_DIR / "sessions.json"
USER_KEYS_PATH = STATE_DIR / "user_keys.json"

SESSION_MAX_AGE = 86400 * 30  # 30 days

APPS = {
    "seedance": {"dir": ROOT.parent / "seedance", "port": int(os.environ.get("SEEDANCE_PORT", "8787"))},
    "nano-banana": {"dir": ROOT.parent / "nano-banana", "port": int(os.environ.get("NANO_PORT", "8797"))},
    "dreamina": {"dir": ROOT.parent / "dreamina", "port": int(os.environ.get("DREAMINA_PORT", "8888"))},
    "volcengine-portrait": {"dir": ROOT.parent / "volcengine-portrait", "port": int(os.environ.get("VOLCENGINE_PORTRAIT_PORT", "8891"))},
}

PORTAL_PORT = int(os.environ.get("PORTAL_PORT", "9090"))
REDIRECT_PORT = int(os.environ.get("REDIRECT_PORT", "9089"))

# Shared secret between portal and sub-apps for the internal finalize callback.
# Generated fresh per portal launch; injected into each sub-app's env so a
# malicious local user cannot retroactively decrement stats without it.
INTERNAL_TOKEN = os.environ.get("PORTAL_INTERNAL_TOKEN") or secrets.token_hex(32)
os.environ["PORTAL_INTERNAL_TOKEN"] = INTERNAL_TOKEN

ROLE_PERMISSIONS: dict[str, set[str]] = {
    "admin": {"use_apps", "view_stats_all", "manage_users", "manage_dreamina_accounts"},
    "user":  {"use_apps", "view_stats_own"},
    "viewer": {"view_stats_own"},
}

# Providers recognized by the key vault (maps to sub-app X-Api-Key / X-Access-Key header)
KEY_PROVIDERS = ["t8star", "volcengine", "volcengine_ak", "gemini", "openai", "other"]


# RFC1918 private + RFC6598 shared (100.64-127) blacklist + IPv4 link-local + 240/4 reserved
# 私有段：192.168/16, 10/8, 172.16-31
# 黑名单：240/4 (GoGoJump 等), 100.64/10 (Tailscale CGNAT), 169.254/16 (link-local), 198.18-19 (benchmarking)
_VPN_BLACKLIST_PREFIXES = ("240.", "169.254.", "198.18.", "198.19.")


def _is_private_ipv4(ip: str) -> bool:
    """Return True if `ip` is in RFC1918 private space (excluding the 100.64/10 CGNAT and VPN-tunnel ranges)."""
    if ip.startswith("192.168.") or ip.startswith("10."):
        return True
    if ip.startswith("172."):
        try:
            second = int(ip.split(".", 2)[1])
        except (ValueError, IndexError):
            return False
        return 16 <= second <= 31
    return False


def _is_vpn_or_reserved(ip: str) -> bool:
    if ip.startswith(_VPN_BLACKLIST_PREFIXES):
        return True
    # 100.64.0.0/10 — CGNAT, used by Tailscale and many VPN providers
    if ip.startswith("100."):
        try:
            second = int(ip.split(".", 2)[1])
        except (ValueError, IndexError):
            return False
        return 64 <= second <= 127
    return False


def get_lan_ip() -> str:
    candidates: list[str] = []
    try:
        if sys.platform == "win32":
            out = subprocess.check_output(["ipconfig"], text=True, stderr=subprocess.DEVNULL, encoding="gbk", errors="replace")
            for line in out.splitlines():
                line = line.strip()
                if "IPv4" in line and "127.0.0.1" not in line:
                    ip = line.rsplit(":", 1)[-1].strip()
                    candidates.append(ip)
        else:
            out = subprocess.check_output(["ifconfig"], text=True, stderr=subprocess.DEVNULL)
            for line in out.splitlines():
                if "inet " in line and "127.0.0.1" not in line:
                    parts = line.strip().split()
                    if len(parts) >= 2:
                        candidates.append(parts[1])
    except Exception:
        pass
    # Prefer private addresses; skip VPN/reserved blacklist.
    for ip in candidates:
        if _is_vpn_or_reserved(ip):
            continue
        if _is_private_ipv4(ip):
            return ip
    # Fallback: outbound socket trick. Apply same filters; if blacklisted, fall through.
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(2.0)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if _is_private_ipv4(ip) and not _is_vpn_or_reserved(ip):
            return ip
        print(f"  [WARN] outbound IP {ip} blacklisted (VPN/reserved), falling back to 127.0.0.1", flush=True)
    except Exception:
        pass
    return "127.0.0.1"


def _find_openssl() -> str | None:
    which = shutil.which("openssl")
    if which:
        return which
    if sys.platform == "win32":
        for p in [r"C:\Program Files\Git\usr\bin\openssl.exe", r"C:\Program Files\OpenSSL\bin\openssl.exe"]:
            if Path(p).exists():
                return p
    return None


def ensure_certs(cert_dir: Path) -> tuple[Path, Path] | None:
    cert_file = cert_dir / "cert.pem"
    key_file = cert_dir / "key.pem"
    ip_file = cert_dir / "lan_ip.txt"
    current_ip = get_lan_ip()
    if cert_file.exists() and key_file.exists():
        if ip_file.exists():
            if ip_file.read_text().strip() != current_ip:
                print(f"  LAN IP changed, regenerating cert...")
                cert_file.unlink(missing_ok=True)
                key_file.unlink(missing_ok=True)
            else:
                return cert_file, key_file
        else:
            ip_file.write_text(current_ip)
            return cert_file, key_file
    openssl = _find_openssl()
    if not openssl:
        print("  [WARN] openssl not found — running in HTTP-only mode")
        return None
    cert_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        openssl, "req", "-x509", "-newkey", "rsa:2048",
        "-keyout", str(key_file), "-out", str(cert_file),
        "-days", "365", "-nodes",
        "-subj", "/CN=AI Generation Portal",
        "-addext", f"subjectAltName=DNS:localhost,IP:127.0.0.1,IP:{current_ip}"
    ], check=True, capture_output=True)
    ip_file.write_text(current_ip)
    print(f"  Generated self-signed certificate (LAN IP: {current_ip})")
    return cert_file, key_file


# ─── Auth ──────────────────────────────────────────────────────────────────────

class AuthManager:
    def __init__(self):
        self._lock = threading.Lock()

    def _load_users(self) -> dict:
        if USERS_PATH.exists():
            try:
                return json.loads(USERS_PATH.read_text("utf-8"))
            except Exception:
                pass
        return {"users": []}

    def _save_users(self, data: dict):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        USERS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")

    def _load_sessions(self) -> dict:
        if SESSIONS_PATH.exists():
            try:
                return json.loads(SESSIONS_PATH.read_text("utf-8"))
            except Exception:
                pass
        return {"sessions": {}}

    def _save_sessions(self, data: dict):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        SESSIONS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")

    def _hash_pw(self, pw: str) -> str:
        salt = os.urandom(16)
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, 260000)
        return f"pbkdf2:sha256:260000:{salt.hex()}:{dk.hex()}"

    def _verify_pw(self, pw: str, stored: str) -> bool:
        try:
            _, _, iters_str, salt_hex, dk_hex = stored.split(":")
            dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(salt_hex), int(iters_str))
            return secrets.compare_digest(dk.hex(), dk_hex)
        except Exception:
            return False

    def first_run(self) -> bool:
        with self._lock:
            return len(self._load_users()["users"]) == 0

    def create_user(self, username: str, pw: str, role: str = "user") -> dict | None:
        with self._lock:
            data = self._load_users()
            if any(u["username"] == username for u in data["users"]):
                return None
            user = {
                "id": str(uuid.uuid4()), "username": username,
                "pw_hash": self._hash_pw(pw), "role": role,
                "enabled": True, "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            data["users"].append(user)
            self._save_users(data)
            return user

    def login(self, username: str, pw: str) -> str | None:
        with self._lock:
            data = self._load_users()
            user = next((u for u in data["users"] if u["username"] == username and u.get("enabled", True)), None)
            if not user or not self._verify_pw(pw, user["pw_hash"]):
                return None
            token = secrets.token_hex(32)
            sess = self._load_sessions()
            now = time.time()
            sess["sessions"] = {k: v for k, v in sess["sessions"].items() if v.get("expires", 0) > now}
            sess["sessions"][token] = {
                "user_id": user["id"], "username": user["username"],
                "role": user["role"], "expires": now + SESSION_MAX_AGE,
            }
            self._save_sessions(sess)
            return token

    def logout(self, token: str):
        with self._lock:
            sess = self._load_sessions()
            sess["sessions"].pop(token, None)
            self._save_sessions(sess)

    def get_user(self, token: str) -> dict | None:
        if not token:
            return None
        with self._lock:
            sess = self._load_sessions()
            s = sess["sessions"].get(token)
            if not s or s.get("expires", 0) < time.time():
                return None
            return {"user_id": s["user_id"], "username": s["username"], "role": s["role"]}

    def list_users(self) -> list:
        with self._lock:
            data = self._load_users()
            return [{"id": u["id"], "username": u["username"], "role": u["role"], "enabled": u.get("enabled", True)}
                    for u in data["users"]]

    def update_user(self, user_id: str, **kwargs) -> bool:
        allowed = {"role", "enabled"}
        with self._lock:
            data = self._load_users()
            for u in data["users"]:
                if u["id"] == user_id:
                    for k, v in kwargs.items():
                        if k in allowed:
                            u[k] = v
                    self._save_users(data)
                    return True
            return False

    def reset_password(self, user_id: str, new_pw: str) -> bool:
        with self._lock:
            data = self._load_users()
            for u in data["users"]:
                if u["id"] == user_id:
                    u["pw_hash"] = self._hash_pw(new_pw)
                    self._save_users(data)
                    return True
            return False

    def has_permission(self, user: dict, perm: str) -> bool:
        return perm in ROLE_PERMISSIONS.get(user.get("role", ""), set())

    def signup_enabled(self) -> bool:
        with self._lock:
            return bool(self._load_users().get("signup_enabled", True))

    def set_signup_enabled(self, enabled: bool):
        with self._lock:
            data = self._load_users()
            data["signup_enabled"] = bool(enabled)
            self._save_users(data)


# ─── Key Vault ─────────────────────────────────────────────────────────────────

class KeyManager:
    def __init__(self):
        self._lock = threading.Lock()

    def _load(self) -> dict:
        if USER_KEYS_PATH.exists():
            try:
                return json.loads(USER_KEYS_PATH.read_text("utf-8"))
            except Exception:
                pass
        return {"keys": []}

    def _save(self, data: dict):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        USER_KEYS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")

    @staticmethod
    def _mask(k: str) -> str:
        if len(k) > 10:
            return k[:6] + "..." + k[-4:]
        return "***"

    def list_keys(self, user_id: str, provider: str | None = None) -> list[dict]:
        with self._lock:
            data = self._load()
            keys = [k for k in data["keys"] if k["user_id"] == user_id]
            if provider:
                keys = [k for k in keys if k["provider"] == provider]
            return [{"id": k["id"], "name": k["name"], "provider": k["provider"],
                     "note": k.get("note", ""), "key_hint": self._mask(k["key"])} for k in keys]

    def add_key(self, user_id: str, name: str, provider: str, key: str, note: str = "") -> dict:
        # The volcengine-portrait subapp uses a single company-wide key managed by admin
        # (POST /api/platform/portrait-key). Per-user personal keys are explicitly disabled
        # to avoid drift between accounts.
        if provider == "volcengine-portrait":
            raise ValueError("人像生成由 admin 统一配置,不支持个人密钥")
        with self._lock:
            data = self._load()
            entry = {
                "id": str(uuid.uuid4()), "user_id": user_id, "name": name,
                "provider": provider, "key": key, "note": note,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            data["keys"].append(entry)
            self._save(data)
            return {"id": entry["id"], "name": entry["name"], "provider": entry["provider"],
                    "note": entry["note"], "key_hint": self._mask(key)}

    def delete_key(self, user_id: str, key_id: str) -> bool:
        with self._lock:
            data = self._load()
            before = len(data["keys"])
            data["keys"] = [k for k in data["keys"] if not (k["id"] == key_id and k["user_id"] == user_id)]
            if len(data["keys"]) < before:
                self._save(data)
                return True
            return False

    def resolve(self, user_id: str, key_id: str) -> str | None:
        """Return plaintext key value. Used only server-side."""
        with self._lock:
            data = self._load()
            k = next((k for k in data["keys"] if k["id"] == key_id and k["user_id"] == user_id), None)
            return k["key"] if k else None


# ─── App Manager ───────────────────────────────────────────────────────────────

class AppManager:
    def __init__(self):
        self.processes: dict[str, subprocess.Popen] = {}
        self.log_handles: dict[str, Any] = {}
        self.status: dict[str, dict[str, Any]] = {}
        self._unhealthy_strikes: dict[str, int] = {}
        self._stop_event = threading.Event()

    def _kill_port_squatter(self, port: int):
        """Kill any process holding `port` that we didn't start ourselves.
        Retries once if a listener still survives after the first SIGKILL pass —
        macOS launchd 偶尔会先收养孤儿进程导致首轮 lsof 抢在 launchd 完成 reap 之前。"""
        lsof_candidates = ["/usr/sbin/lsof", "/usr/bin/lsof", "/opt/homebrew/bin/lsof", "lsof"]

        def _query_listeners() -> list[int] | None:
            """Return a list of PIDs holding the port in LISTEN; None if lsof unavailable."""
            for lsof_bin in lsof_candidates:
                try:
                    out = subprocess.check_output(
                        [lsof_bin, "-nP", "-tiTCP:" + str(port), "-sTCP:LISTEN"],
                        stderr=subprocess.DEVNULL, text=True, timeout=5,
                    )
                    pids: list[int] = []
                    for line in out.strip().splitlines():
                        try:
                            pids.append(int(line.strip()))
                        except ValueError:
                            continue
                    return pids
                except subprocess.CalledProcessError:
                    return []  # lsof ran cleanly, no listeners
                except (subprocess.TimeoutExpired, FileNotFoundError):
                    continue
            return None

        for attempt in range(2):
            pids = _query_listeners()
            if pids is None:
                print(f"  [port-cleanup] WARNING: lsof not found, cannot probe port {port}", flush=True)
                return
            own_pids = {proc.pid for proc in self.processes.values() if proc.poll() is None}
            killed_any = False
            for pid in pids:
                if pid in own_pids or pid == os.getpid():
                    continue
                try:
                    os.kill(pid, signal.SIGKILL)
                    killed_any = True
                    print(f"  [port-cleanup] killed orphan PID {pid} on port {port}"
                          + (f" (retry)" if attempt > 0 else ""), flush=True)
                except (ProcessLookupError, PermissionError):
                    pass
            if not killed_any:
                return
            time.sleep(0.5 if attempt == 0 else 1.0)

    def start_all(self):
        for name, config in APPS.items():
            self.start_app(name, config)
        threading.Thread(target=self._health_loop, daemon=True).start()
        threading.Thread(target=self._log_rotation_loop, daemon=True).start()

    def _log_rotation_loop(self):
        log_dir = STATE_DIR / "logs"
        max_age = 7 * 86400
        while not self._stop_event.is_set():
            try:
                if log_dir.exists():
                    now = time.time()
                    for log_path in log_dir.glob("*.log"):
                        try:
                            if log_path.stat().st_size == 0:
                                continue
                            if now - log_path.stat().st_mtime < max_age:
                                continue
                            size_mb = log_path.stat().st_size / 1024 / 1024
                            with log_path.open("wb"):
                                pass
                            print(f"  [log-rotate] truncated {log_path.name} ({size_mb:.1f} MB, >7d old)", flush=True)
                        except OSError as exc:
                            print(f"  [log-rotate] {log_path.name} skipped: {exc}", flush=True)
            except Exception as exc:
                print(f"  [log-rotate] loop error: {exc}", flush=True)
            self._stop_event.wait(3600)

    def _read_portrait_keys(self) -> tuple[str, str]:
        """Read volcengine AK/SK from the portrait sub-app's config.json so
        seedance can inherit the same credentials for TOS uploads. Portrait
        stores keys at <portrait_dir>/config.json (note: not in state/, see
        volcengine-portrait/app.py load_config). Empty on any failure."""
        try:
            cfg_path = APPS["volcengine-portrait"]["dir"] / "config.json"
            if not cfg_path.exists():
                return "", ""
            data = json.loads(cfg_path.read_text("utf-8"))
            return (data.get("access_key") or "").strip(), (data.get("secret_key") or "").strip()
        except Exception:
            return "", ""

    def start_app(self, name: str, config: dict):
        app_dir = config["dir"]
        if not (app_dir / "app.py").exists():
            self.status[name] = {"status": "missing", "error": "app.py not found"}
            return
        self._kill_port_squatter(config["port"])
        env = os.environ.copy()
        env["PORT"] = str(config["port"])
        env["HOST"] = "127.0.0.1"
        env["CORS"] = "1"
        if "DATA_DIR" in os.environ:
            env["DATA_DIR"] = str(app_dir / "test-data")
        if name in ("seedance", "volcengine-portrait"):
            ak, sk = self._read_portrait_keys()
            if ak and sk:
                env["TOS_ACCESS_KEY"] = ak
                env["TOS_SECRET_KEY"] = sk
        old_log = self.log_handles.pop(name, None)
        if old_log:
            try: old_log.close()
            except Exception: pass
        log_dir = STATE_DIR / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = (log_dir / f"{name}.log").open("ab", buffering=0)
        popen_kwargs = dict(_POPEN_EXTRA)
        if hasattr(os, "setsid"):
            popen_kwargs["preexec_fn"] = os.setsid
        try:
            proc = subprocess.Popen(
                [sys.executable, "app.py"], cwd=str(app_dir), env=env,
                stdout=log_file, stderr=subprocess.STDOUT, **popen_kwargs,
            )
        except Exception as exc:
            log_file.close()
            self.status[name] = {"status": "crashed", "error": str(exc), "port": config["port"]}
            return
        self.log_handles[name] = log_file
        self.processes[name] = proc
        self.status[name] = {"status": "starting", "port": config["port"], "pid": proc.pid}
        self._unhealthy_strikes[name] = 0

    def _tcp_probe(self, port: int, timeout: float = 3.0) -> bool:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect(("127.0.0.1", port))
            return True
        except (OSError, socket.timeout):
            return False
        finally:
            try: s.close()
            except Exception: pass

    def _health_loop(self):
        while not self._stop_event.is_set():
            time.sleep(15)
            for name, config in APPS.items():
                proc = self.processes.get(name)
                if proc and proc.poll() is not None:
                    self.status[name] = {"status": "crashed", "exit_code": proc.returncode, "port": config["port"]}
                    print(f"  [watchdog] {name} exited (code {proc.returncode}), restarting")
                    self.start_app(name, config)
                    continue
                alive = self._tcp_probe(config["port"])
                if alive:
                    self.status[name] = {"status": "ready", "port": config["port"], "pid": proc.pid if proc else None}
                    self._unhealthy_strikes[name] = 0
                else:
                    self._unhealthy_strikes[name] = self._unhealthy_strikes.get(name, 0) + 1
                    self.status[name] = {"status": "unhealthy", "port": config["port"],
                                         "strikes": self._unhealthy_strikes[name]}
                    if self._unhealthy_strikes[name] >= 3:
                        print(f"  [watchdog] {name} unhealthy for 3 cycles, restarting")
                        if proc and proc.poll() is None:
                            try: proc.kill()
                            except Exception: pass
                        self.start_app(name, config)

    def shutdown(self):
        self._stop_event.set()
        for _, proc in self.processes.items():
            if proc.poll() is None:
                if hasattr(os, "killpg"):
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                        continue
                    except (ProcessLookupError, PermissionError):
                        pass
                proc.terminate()
        for _, proc in self.processes.items():
            try: proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                if hasattr(os, "killpg"):
                    try: os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except Exception: pass
                proc.kill()
        for handle in self.log_handles.values():
            try: handle.close()
            except Exception: pass
        self.log_handles.clear()


# ─── Usage Tracker ─────────────────────────────────────────────────────────────

def _append_usage_jsonl(entry: dict, today: str):
    """Append a single usage entry to state/logs/usage-YYYY-MM-DD.jsonl.
    Failures are logged but do NOT propagate — primary usage.json save must not be blocked."""
    try:
        logs_dir = STATE_DIR / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        path = logs_dir / f"usage-{today}.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:
        print(f"  [usage] jsonl append failed for {today}: {exc}", flush=True)


def _prune_old_usage_jsonl(today: str):
    """Delete usage-*.jsonl older than USAGE_JSONL_RETENTION_DAYS. Best-effort."""
    try:
        logs_dir = STATE_DIR / "logs"
        if not logs_dir.exists():
            return
        cutoff = datetime.strptime(today, "%Y-%m-%d") - timedelta(days=USAGE_JSONL_RETENTION_DAYS)
        for p in logs_dir.glob("usage-*.jsonl"):
            try:
                d = datetime.strptime(p.stem[len("usage-"):], "%Y-%m-%d")
                if d < cutoff:
                    p.unlink()
            except Exception:
                continue
    except Exception:
        pass


class UsageTracker:
    def __init__(self):
        self._lock = threading.Lock()
        self._data = self._load()
        self._pending_jobs: list[dict] = []
        threading.Thread(target=self._job_poll_loop, daemon=True).start()
        threading.Thread(target=self._auto_backup_loop, daemon=True).start()

    @staticmethod
    def _empty_data() -> dict[str, Any]:
        return {"records": [], "daily": {}, "by_user": {}, "job_owners": {}}

    @staticmethod
    def _try_parse(path) -> dict[str, Any] | None:
        try:
            if not path.exists():
                return None
            return json.loads(path.read_text("utf-8"))
        except Exception:
            return None

    def _load(self) -> dict[str, Any]:
        """Load usage data with self-healing from .bak if the primary is corrupt.
        Long-term retention: never drop date keys here."""
        bak_path = USAGE_PATH.with_suffix(USAGE_PATH.suffix + ".bak")
        primary = self._try_parse(USAGE_PATH)
        if primary is not None and isinstance(primary, dict):
            d = primary
        else:
            backup = self._try_parse(bak_path)
            if backup is not None and isinstance(backup, dict):
                # Quarantine the corrupt primary so a future _save doesn't clobber the good .bak
                if USAGE_PATH.exists():
                    quarantine = USAGE_PATH.with_name(f"usage.corrupt.{int(time.time())}.json")
                    try:
                        USAGE_PATH.rename(quarantine)
                        print(f"  [usage] primary corrupt, quarantined to {quarantine.name}, recovered from .bak", flush=True)
                    except Exception:
                        pass
                d = backup
            else:
                if USAGE_PATH.exists() or bak_path.exists():
                    print("  [usage] both primary and .bak unreadable, starting fresh (existing files left in place)", flush=True)
                return self._empty_data()
        # Schema-tolerant defaults — never remove existing keys
        d.setdefault("records", [])
        d.setdefault("daily", {})
        d.setdefault("by_user", {})
        d.setdefault("job_owners", {})
        return d

    def _save(self):
        """Atomic double-write: tmp → primary (rename), then copy primary → .bak.
        Long-term retention: do NOT prune any date keys here."""
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self._data, ensure_ascii=False, indent=2)
        tmp_path = USAGE_PATH.with_suffix(USAGE_PATH.suffix + ".tmp")
        bak_path = USAGE_PATH.with_suffix(USAGE_PATH.suffix + ".bak")
        try:
            tmp_path.write_text(payload, "utf-8")
            os.replace(tmp_path, USAGE_PATH)
        except Exception as exc:
            print(f"  [usage] _save primary write failed: {exc}", flush=True)
            return
        # Refresh .bak (best-effort; failure here doesn't compromise primary)
        try:
            shutil.copyfile(USAGE_PATH, bak_path)
        except Exception:
            pass

    def _auto_backup_loop(self):
        """Every 6 hours, re-sync .bak from primary if primary is parseable."""
        while True:
            time.sleep(6 * 3600)
            try:
                if USAGE_PATH.exists() and self._try_parse(USAGE_PATH) is not None:
                    bak_path = USAGE_PATH.with_suffix(USAGE_PATH.suffix + ".bak")
                    shutil.copyfile(USAGE_PATH, bak_path)
            except Exception:
                pass

    def record(self, app: str, client_ip: str, method: str, path: str, username: str = ""):
        today = time.strftime("%Y-%m-%d")
        entry = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "app": app, "ip": client_ip,
                 "username": username, "method": method, "path": path}
        with self._lock:
            self._data["records"].append(entry)
            if len(self._data["records"]) > 2000:
                self._data["records"] = self._data["records"][-1000:]
            day_stats = self._data["daily"].setdefault(today, {})
            app_stats = day_stats.setdefault(app, {"requests": 0, "jobs": 0})
            app_stats["requests"] += 1
            self._save()
        # Below the lock: jsonl append + prune are best-effort. Helpers already
        # swallow errors internally; the outer try/except is a second layer so
        # even a monkey-patched replacement (tests) cannot break usage.json save.
        try:
            _append_usage_jsonl(entry, today)
        except Exception:
            pass
        try:
            _prune_old_usage_jsonl(today)
        except Exception:
            pass

    def register_job(self, app: str, job_id: str, username: str, job_type: str = "image", duration_per_item: int = 0):
        with self._lock:
            self._pending_jobs.append({
                "app": app, "job_id": job_id, "username": username,
                "job_type": job_type, "duration_per_item": duration_per_item,
                "submitted_at": time.time(), "date": time.strftime("%Y-%m-%d"),
            })
            owners = self._data.setdefault("job_owners", {})
            owners[f"{app}:{job_id}"] = {"username": username, "ts": time.time()}
            # cap to last 5000 to bound disk growth
            if len(owners) > 5000:
                kept = sorted(owners.items(), key=lambda kv: kv[1].get("ts", 0))[-3000:]
                self._data["job_owners"] = dict(kept)
            self._save()

    def get_job_owner(self, app: str, job_id: str) -> str:
        with self._lock:
            o = self._data.get("job_owners", {}).get(f"{app}:{job_id}")
            return o.get("username", "") if isinstance(o, dict) else ""

    def _job_poll_loop(self):
        while True:
            time.sleep(15)
            with self._lock:
                jobs = list(self._pending_jobs)
            if not jobs:
                continue
            done_ids = []
            for job in jobs:
                try:
                    port = APPS[job["app"]]["port"]
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                    conn.request("GET", f"/api/jobs/{job['job_id']}")
                    resp = conn.getresponse()
                    if resp.status == 200:
                        data = json.loads(resp.read())
                        # dreamina nests fields under "job"; tracker prefers top-level
                        # (handle_job_status flattens them) but fall back for safety.
                        nested = data.get("job") if isinstance(data.get("job"), dict) else {}
                        status = data.get("status") or nested.get("status") or ""
                        if status in ("succeeded", "failed", "completed"):
                            done = max(1, int(data.get("done") or nested.get("done") or 0))
                            # Re-derive job_type from response when possible — dreamina
                            # retry endpoints may have been misclassified at register time.
                            # Only override if task_type carries explicit signal; otherwise
                            # keep the job_type set at register time (e.g. volcengine-portrait
                            # uses "virtual"/"real" which conveys no image/video info).
                            task_type = (data.get("task_type") or nested.get("task_type") or "").lower()
                            job_type = job["job_type"]
                            if "video" in task_type or "frame" in task_type:
                                job_type = "video"
                            elif "text2image" in task_type or "image2image" in task_type:
                                job_type = "image"
                            if job_type == "video":
                                # Prefer per-item duration the subapp reports directly.
                                per_item = (int(data.get("duration") or 0)
                                            or int(data.get("duration_seconds") or 0)
                                            or int(nested.get("duration") or 0)
                                            or int(job.get("duration_per_item") or 0))
                                self._add_user_stat(job["date"], job["username"], job["app"], 0, done * per_item)
                            else:
                                self._add_user_stat(job["date"], job["username"], job["app"], done, 0)
                            done_ids.append(job["job_id"])
                    conn.close()
                except Exception:
                    pass
                if time.time() - job["submitted_at"] > 7200:
                    done_ids.append(job["job_id"])
            if done_ids:
                with self._lock:
                    self._pending_jobs = [j for j in self._pending_jobs if j["job_id"] not in done_ids]

    def _add_user_stat(self, date: str, username: str, app: str, images: int, seconds: int):
        with self._lock:
            by_user = self._data.setdefault("by_user", {}).setdefault(date, {})
            u = by_user.setdefault(username, {})
            s = u.setdefault(app, {"images": 0, "seconds": 0})
            s["images"] += images
            s["seconds"] += seconds
            self._save()

    def inc_daily_jobs(self, app: str):
        today = time.strftime("%Y-%m-%d")
        with self._lock:
            day_stats = self._data.setdefault("daily", {}).setdefault(today, {})
            app_stats = day_stats.setdefault(app, {"requests": 0, "jobs": 0})
            app_stats["jobs"] = int(app_stats.get("jobs", 0)) + 1
            self._save()

    def finalize_job(self, app: str, job_id: str, status: str) -> bool:
        """Idempotent: mark a job final. If status indicates failure/cancel,
        roll back the +1 that inc_daily_jobs applied at registration time.
        Returns True if the rollback was applied (i.e. first time we saw
        a failure for this job), False otherwise."""
        status = (status or "").lower()
        is_failure = status in ("failed", "fail", "failure", "cancelled", "canceled", "error")
        with self._lock:
            owners = self._data.setdefault("job_owners", {})
            key = f"{app}:{job_id}"
            owner = owners.get(key)
            if not isinstance(owner, dict):
                return False
            if owner.get("finalized"):
                return False
            owner["finalized"] = True
            owner["final_status"] = status
            rolled = False
            if is_failure:
                ts = owner.get("ts", time.time())
                date = time.strftime("%Y-%m-%d", time.localtime(ts))
                day_stats = self._data.setdefault("daily", {}).setdefault(date, {})
                app_stats = day_stats.setdefault(app, {"requests": 0, "jobs": 0})
                app_stats["jobs"] = max(0, int(app_stats.get("jobs", 0)) - 1)
                rolled = True
                # Drop from pending_jobs so the poll loop does not double-act.
                self._pending_jobs = [
                    j for j in self._pending_jobs if not (j["app"] == app and j["job_id"] == job_id)
                ]
            self._save()
            return rolled

    def _is_job_request(self, method: str, path: str) -> bool:
        if method != "POST":
            return False
        job_patterns = ["/api/jobs", "/api/text2image", "/api/image2image", "/api/text2video",
                        "/api/image2video", "/api/frames2video", "/api/multimodal2video", "/api/multiframe2video",
                        "/api/virtual/jobs", "/api/real/jobs"]
        return any(path.startswith(p) for p in job_patterns)

    def get_stats(self, username: str = "", role: str = "user") -> dict[str, Any]:
        today = time.strftime("%Y-%m-%d")
        with self._lock:
            today_stats = self._data["daily"].get(today, {})
            recent = self._data["records"][-20:]
            by_user_today = self._data.get("by_user", {}).get(today, {})
            if role != "admin" and username:
                by_user_today = {username: by_user_today.get(username, {})}
        total_jobs = sum(v.get("jobs", 0) for v in today_stats.values())
        total_requests = sum(v.get("requests", 0) for v in today_stats.values())
        return {
            "today": today, "today_jobs": total_jobs, "today_requests": total_requests,
            "by_app": today_stats if role == "admin" else {},
            "by_user": by_user_today,
            "recent": recent if role == "admin" else [],
            "daily": dict(list(self._data["daily"].items())[-7:]) if role == "admin" else {},
        }

    def get_day(self, date: str, username: str = "", role: str = "user") -> dict[str, Any]:
        """Return daily + by_user for a specific date string YYYY-MM-DD."""
        with self._lock:
            daily = self._data.get("daily", {}).get(date, {})
            by_user = dict(self._data.get("by_user", {}).get(date, {}))
        if role != "admin" and username:
            by_user = {username: by_user.get(username, {})}
        total_jobs = sum(v.get("jobs", 0) for v in daily.values())
        total_requests = sum(v.get("requests", 0) for v in daily.values())
        return {
            "date": date,
            "total_jobs": total_jobs,
            "total_requests": total_requests,
            "by_app": daily if role == "admin" else {},
            "by_user": by_user,
        }

    def get_history(self, days: int = 30, username: str = "", role: str = "user") -> dict[str, Any]:
        """Return per-user per-app daily values for the last N days.

        Shape:
            {
              "dates":  ["YYYY-MM-DD", ...],          # length=days, ascending, today is last
              "users": {
                "<username>": {
                  "<app>": {"images": [..N..], "seconds": [..N..]},
                  ...
                },
                ...
              }
            }

        Non-admins only see their own row. Missing dates → zero-filled.
        """
        days = max(1, min(int(days or 30), 365))
        # Build ascending date list ending at today
        now_t = time.time()
        dates: list[str] = []
        for i in range(days - 1, -1, -1):
            dates.append(time.strftime("%Y-%m-%d", time.localtime(now_t - i * 86400)))
        date_set = set(dates)
        with self._lock:
            by_user_full = self._data.get("by_user", {})
            # Collect all users that appeared in the window
            user_universe: set[str] = set()
            for d in dates:
                user_universe.update((by_user_full.get(d, {}) or {}).keys())
            if role != "admin":
                user_universe = {username} if username else set()
            users: dict[str, dict[str, dict[str, list[int]]]] = {}
            for u in sorted(user_universe):
                per_app: dict[str, dict[str, list[int]]] = {}
                for d in dates:
                    day_user = (by_user_full.get(d, {}) or {}).get(u, {}) or {}
                    for app_name, stats in day_user.items():
                        slot = per_app.setdefault(app_name, {"images": [0] * len(dates), "seconds": [0] * len(dates)})
                        idx = dates.index(d)
                        slot["images"][idx] = int(stats.get("images", 0) or 0)
                        slot["seconds"][idx] = int(stats.get("seconds", 0) or 0)
                users[u] = per_app
        # Hide dates outside the window (the loop above only writes inside the window)
        _ = date_set
        return {"dates": dates, "users": users}

    @staticmethod
    def _validate_date(s: str) -> str | None:
        """Return YYYY-MM-DD if valid, else None."""
        if not s or len(s) != 10 or s[4] != "-" or s[7] != "-":
            return None
        try:
            time.strptime(s, "%Y-%m-%d")
            return s
        except ValueError:
            return None

    @staticmethod
    def _date_range(start: str, end: str) -> list[str]:
        """Inclusive date list start..end. Caps at 366 days to bound payload."""
        start_t = time.mktime(time.strptime(start, "%Y-%m-%d"))
        end_t = time.mktime(time.strptime(end, "%Y-%m-%d"))
        if end_t < start_t:
            start_t, end_t = end_t, start_t
        days = int((end_t - start_t) // 86400) + 1
        days = max(1, min(days, 366))
        return [time.strftime("%Y-%m-%d", time.localtime(start_t + i * 86400)) for i in range(days)]

    def get_range(self, start: str, end: str, username: str = "", role: str = "user") -> dict[str, Any]:
        """Per-user per-app daily series for any [start, end] window. Same shape as get_history."""
        dates = self._date_range(start, end)
        with self._lock:
            by_user_full = self._data.get("by_user", {})
            user_universe: set[str] = set()
            for d in dates:
                user_universe.update((by_user_full.get(d, {}) or {}).keys())
            if role != "admin":
                user_universe = {username} if username else set()
            users: dict[str, dict[str, dict[str, list[int]]]] = {}
            for u in sorted(user_universe):
                per_app: dict[str, dict[str, list[int]]] = {}
                for idx, d in enumerate(dates):
                    day_user = (by_user_full.get(d, {}) or {}).get(u, {}) or {}
                    for app_name, stats in day_user.items():
                        slot = per_app.setdefault(app_name, {"images": [0] * len(dates), "seconds": [0] * len(dates)})
                        slot["images"][idx] = int(stats.get("images", 0) or 0)
                        slot["seconds"][idx] = int(stats.get("seconds", 0) or 0)
                users[u] = per_app
        return {"dates": dates, "users": users}

    def export_range(self, start: str, end: str, app_names: list[str], username: str = "", role: str = "user") -> list[dict[str, Any]]:
        """Wide-format per-user totals for [start, end].
        Returns list[dict] where each dict has username + per-app images/seconds + totals.
        Order is sorted by username; non-admin sees only their own row.
        """
        dates = self._date_range(start, end)
        date_set = set(dates)
        with self._lock:
            by_user_full = self._data.get("by_user", {})
            user_universe: set[str] = set()
            for d in dates:
                user_universe.update((by_user_full.get(d, {}) or {}).keys())
            if role != "admin":
                user_universe = {username} if username else set()
            rows: list[dict[str, Any]] = []
            for u in sorted(user_universe):
                totals: dict[str, dict[str, int]] = {a: {"images": 0, "seconds": 0} for a in app_names}
                for d in dates:
                    day_user = (by_user_full.get(d, {}) or {}).get(u, {}) or {}
                    for app_name, stats in day_user.items():
                        slot = totals.setdefault(app_name, {"images": 0, "seconds": 0})
                        slot["images"] += int(stats.get("images", 0) or 0)
                        slot["seconds"] += int(stats.get("seconds", 0) or 0)
                row: dict[str, Any] = {"username": u}
                grand_images = 0
                grand_seconds = 0
                for a in app_names:
                    row[f"{a}_images"] = totals[a]["images"]
                    row[f"{a}_seconds"] = totals[a]["seconds"]
                    grand_images += totals[a]["images"]
                    grand_seconds += totals[a]["seconds"]
                # Include any extra apps that appeared in the window but aren't in app_names
                for a, v in totals.items():
                    if a in app_names: continue
                    row[f"{a}_images"] = v["images"]
                    row[f"{a}_seconds"] = v["seconds"]
                    grand_images += v["images"]
                    grand_seconds += v["seconds"]
                row["total_images"] = grand_images
                row["total_seconds"] = grand_seconds
                rows.append(row)
        _ = date_set
        return rows


# ─── Singletons ────────────────────────────────────────────────────────────────

auth = AuthManager()
key_manager = KeyManager()
manager = AppManager()
tracker = UsageTracker()


# ─── HTTP Handler ──────────────────────────────────────────────────────────────

_AUTH_EXEMPT = {"/login", "/api/auth/login", "/api/auth/register"}


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except ssl.SSLError as e:
            print(f"  [SSL ERROR] {e}")
        except OSError as e:
            print(f"  [OS ERROR] {e}")

    # ── Auth helpers ──────────────────────────────────────────────────────────

    def _current_user(self) -> dict | None:
        for part in self.headers.get("Cookie", "").split(";"):
            part = part.strip()
            if part.startswith("session="):
                return auth.get_user(part[8:].strip())
        return None

    def _is_https(self) -> bool:
        proto = self.headers.get("X-Forwarded-Proto", "")
        if proto == "https":
            return True
        try:
            return bool(self.connection.context)
        except Exception:
            return False

    def _set_cookie(self, token: str):
        cookie = f"session={token}; Path=/; Max-Age={SESSION_MAX_AGE}; HttpOnly; SameSite=Lax"
        if self._is_https():
            cookie += "; Secure"
        self.send_header("Set-Cookie", cookie)

    def _clear_cookie(self):
        self.send_header("Set-Cookie", "session=; Path=/; Max-Age=0; HttpOnly")

    def _require_auth(self, path: str) -> dict | None:
        """Return user dict or send error/redirect and return None."""
        user = self._current_user()
        if user:
            return user
        if path.startswith("/api/"):
            self._json(401, {"ok": False, "error": "unauthorized"})
        else:
            next_url = urllib.parse.quote(self.path, safe="")
            self._redirect(f"/login?next={next_url}")
        return None

    # ── Request dispatch ──────────────────────────────────────────────────────

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/auth/first-run":
            self._json(200, {"ok": True, "first_run": auth.first_run(), "signup_enabled": auth.signup_enabled()})
            return
        if path in _AUTH_EXEMPT or path == "/login":
            self._serve_portal(path if path != "/login" else "/login.html")
            return
        user = self._require_auth(path)
        if not user:
            return
        if path == "/api/platform/status":
            self._platform_status(user)
        elif path == "/api/platform/stats":
            self._platform_stats(user)
        elif path == "/api/platform/stats/history":
            self._platform_stats_history(user)
        elif path == "/api/platform/stats/day":
            self._platform_stats_day(user)
        elif path == "/api/platform/stats/range":
            self._platform_stats_range(user)
        elif path == "/api/platform/stats/export":
            self._platform_stats_export(user)
        elif path == "/api/platform/activity":
            self._platform_activity(user)
        elif path == "/api/platform/portrait-key":
            self._platform_portrait_key_get(user)
        elif path == "/api/feishu/config":
            self._feishu_config_get(user)
        elif path.startswith("/api/reports/daily/") and path.endswith(".csv"):
            date = path[len("/api/reports/daily/"):-len(".csv")]
            self._report_csv_download(user, date)
        elif path == "/api/auth/me":
            self._json(200, {"ok": True, "username": user["username"], "role": user["role"]})
        elif path == "/api/users":
            if not auth.has_permission(user, "manage_users"):
                self._json(403, {"ok": False, "error": "forbidden"})
            else:
                self._json(200, {"ok": True, "users": auth.list_users()})
        elif path.startswith("/api/keys/") and path.endswith("/reveal"):
            key_id = path[len("/api/keys/"):-len("/reveal")]
            value = key_manager.resolve(user["user_id"], key_id)
            if value is None:
                self._json(404, {"ok": False, "error": "key not found"})
            else:
                self._json(200, {"ok": True, "key": value})
        elif path.startswith("/api/keys"):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            provider = qs.get("provider", [None])[0]
            self._json(200, {"ok": True, "keys": key_manager.list_keys(user["user_id"], provider)})
        elif self._try_proxy(path, "GET", user):
            pass
        else:
            self._serve_portal(path)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/auth/login":
            self._auth_login()
            return
        if path == "/api/auth/register":
            self._auth_register()
            return
        if path == "/api/internal/jobs/finalize":
            self._internal_finalize_job()
            return
        user = self._require_auth(path)
        if not user:
            return
        if path == "/api/auth/logout":
            token = ""
            for part in self.headers.get("Cookie", "").split(";"):
                p = part.strip()
                if p.startswith("session="):
                    token = p[8:].strip()
            auth.logout(token)
            self.send_response(200)
            self._clear_cookie()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "12")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
            return
        if path == "/api/auth/create-user":
            if not auth.has_permission(user, "manage_users"):
                self._json(403, {"ok": False, "error": "forbidden"})
                return
            body = self._read_json()
            if body is None:
                return
            u = auth.create_user(body.get("username", ""), body.get("password", ""), body.get("role", "user"))
            if u:
                self._json(200, {"ok": True, "user": {"id": u["id"], "username": u["username"], "role": u["role"]}})
            else:
                self._json(409, {"ok": False, "error": "username already exists"})
            return
        if path == "/api/auth/signup-toggle":
            if not auth.has_permission(user, "manage_users"):
                self._json(403, {"ok": False, "error": "forbidden"})
                return
            body = self._read_json()
            if body is None:
                return
            auth.set_signup_enabled(bool(body.get("enabled", True)))
            self._json(200, {"ok": True, "signup_enabled": auth.signup_enabled()})
            return
        if path.startswith("/api/users/") and auth.has_permission(user, "manage_users"):
            self._update_user(path)
            return
        if path == "/api/keys":
            body = self._read_json()
            if body is None:
                return
            try:
                entry = key_manager.add_key(
                    user["user_id"], body.get("name", ""), body.get("provider", "other"),
                    body.get("key", ""), body.get("note", ""),
                )
            except ValueError as exc:
                self._json(400, {"ok": False, "error": str(exc)})
                return
            self._json(200, {"ok": True, "key": entry})
            return
        if path == "/api/platform/portrait-key":
            self._platform_portrait_key_set(user)
            return
        if path == "/api/reports/send":
            self._report_send(user)
            return
        if path == "/api/reports/preview":
            self._report_preview(user)
            return
        if path == "/api/feishu/config":
            self._feishu_config_put(user)
            return
        if not self._try_proxy(path, "POST", user):
            self._json(404, {"ok": False, "error": "not found"})

    def do_DELETE(self):
        path = urllib.parse.urlparse(self.path).path
        user = self._require_auth(path)
        if not user:
            return
        if path.startswith("/api/keys/"):
            key_id = path[len("/api/keys/"):]
            if key_manager.delete_key(user["user_id"], key_id):
                self._json(200, {"ok": True})
            else:
                self._json(404, {"ok": False, "error": "not found"})
            return
        if not self._try_proxy(path, "DELETE", user):
            self._json(404, {"ok": False, "error": "not found"})

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ── Auth endpoint handlers ────────────────────────────────────────────────

    def _auth_login(self):
        body = self._read_json()
        if body is None:
            return
        token = auth.login(body.get("username", ""), body.get("password", ""))
        if not token:
            self._json(401, {"ok": False, "error": "invalid credentials"})
            return
        user = auth.get_user(token)
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._set_cookie(token)
        self._cors_headers()
        raw = json.dumps({"ok": True, "username": user["username"], "role": user["role"]}).encode()
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _auth_register(self):
        # First user always allowed (becomes admin); subsequent registrations
        # require signup_enabled flag (default true, admin can toggle off).
        first_run = auth.first_run()
        if not first_run and not auth.signup_enabled():
            self._json(403, {"ok": False, "error": "registration disabled"})
            return
        body = self._read_json()
        if body is None:
            return
        username = body.get("username", "").strip()
        pw = body.get("password", "")
        if not username or not pw or len(pw) < 6:
            self._json(400, {"ok": False, "error": "username required, password min 6 chars"})
            return
        role = "admin" if first_run else "user"
        user = auth.create_user(username, pw, role=role)
        if not user:
            self._json(409, {"ok": False, "error": "username already exists"})
            return
        token = auth.login(username, pw)
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._set_cookie(token)
        self._cors_headers()
        raw = json.dumps({"ok": True, "username": username, "role": role}).encode()
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _update_user(self, path: str):
        # PATCH-like via POST /api/users/<id>
        user_id = path[len("/api/users/"):]
        body = self._read_json()
        if body is None:
            return
        if "password" in body:
            auth.reset_password(user_id, body["password"])
        updates = {k: body[k] for k in ("role", "enabled") if k in body}
        if updates:
            auth.update_user(user_id, **updates)
        self._json(200, {"ok": True})

    # ── Platform endpoints ────────────────────────────────────────────────────

    def _platform_status(self, user: dict):
        lan_ip = get_lan_ip()
        apps_info = []
        for name, config in APPS.items():
            info = {"name": name, "port": config["port"], **manager.status.get(name, {"status": "unknown"})}
            info["url"] = f"/{name}/"
            apps_info.append(info)
        self._json(200, {"ok": True, "lan_ip": lan_ip, "portal_port": PORTAL_PORT, "apps": apps_info})

    def _platform_stats(self, user: dict):
        stats = tracker.get_stats(username=user["username"], role=user["role"])
        self._json(200, {"ok": True, **stats})

    def _platform_stats_history(self, user: dict):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        try:
            days = int(qs.get("days", ["30"])[0])
        except (TypeError, ValueError):
            days = 30
        history = tracker.get_history(days=days, username=user["username"], role=user["role"])
        self._json(200, {"ok": True, **history})

    def _platform_stats_day(self, user: dict):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        date = (qs.get("date", [""])[0] or time.strftime("%Y-%m-%d")).strip()
        # Basic shape sanity (YYYY-MM-DD, 10 chars). Reject anything else.
        if len(date) != 10 or date[4] != "-" or date[7] != "-":
            self._json(400, {"ok": False, "error": "invalid date"})
            return
        day = tracker.get_day(date, username=user["username"], role=user["role"])
        self._json(200, {"ok": True, **day})

    def _parse_range_qs(self) -> tuple[str, str] | None:
        """Read & validate ?start=&end=. Defaults to last 30 days."""
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        end = (qs.get("end", [""])[0] or time.strftime("%Y-%m-%d")).strip()
        default_start = time.strftime("%Y-%m-%d", time.localtime(time.time() - 29 * 86400))
        start = (qs.get("start", [""])[0] or default_start).strip()
        if not UsageTracker._validate_date(start) or not UsageTracker._validate_date(end):
            self._json(400, {"ok": False, "error": "invalid date format (expect YYYY-MM-DD)"})
            return None
        return start, end

    def _platform_stats_range(self, user: dict):
        rng = self._parse_range_qs()
        if not rng:
            return
        start, end = rng
        data = tracker.get_range(start, end, username=user["username"], role=user["role"])
        self._json(200, {"ok": True, "start": start, "end": end, **data})

    def _platform_stats_export(self, user: dict):
        rng = self._parse_range_qs()
        if not rng:
            return
        start, end = rng
        # Wide-format: one row per user, columns per app + totals
        app_names = list(APPS.keys())
        rows = tracker.export_range(start, end, app_names, username=user["username"], role=user["role"])
        # Build CSV with UTF-8 BOM so Excel/WPS double-click renders Chinese correctly.
        buf = io.StringIO()
        # Header row order: username, <app>_images, <app>_seconds for each app, total_images, total_seconds
        fieldnames = ["username"]
        for a in app_names:
            fieldnames.append(f"{a}_images")
            fieldnames.append(f"{a}_seconds")
        # Tail: any extra apps that may have appeared but aren't in APPS list (defensive)
        extra_keys: list[str] = []
        for r in rows:
            for k in r.keys():
                if k in fieldnames or k in ("total_images", "total_seconds"): continue
                if k not in extra_keys: extra_keys.append(k)
        for k in extra_keys:
            fieldnames.append(k)
        fieldnames.extend(["total_images", "total_seconds"])
        writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
        body = ("﻿" + buf.getvalue()).encode("utf-8")
        filename = f"usage-{start}-{end}.csv"
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _platform_portrait_key_get(self, user: dict):
        """Admin-only: returns whether the company-wide volcengine-portrait key is set.
        Never returns the plaintext."""
        if user.get("role") != "admin":
            self._json(403, {"ok": False, "error": "admin only"})
            return
        try:
            port = APPS["volcengine-portrait"]["port"]
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/api/config")
            resp = conn.getresponse()
            data = json.loads(resp.read()) if resp.status == 200 else {}
            conn.close()
        except Exception as exc:
            self._json(502, {"ok": False, "error": f"upstream error: {exc}"})
            return
        self._json(200, {
            "ok": True,
            "has_api_key": bool(data.get("has_api_key") or data.get("has_key")),
            "has_access_key": bool(data.get("has_access_key")),
            "has_secret_key": bool(data.get("has_secret_key")),
        })

    def _platform_portrait_key_set(self, user: dict):
        """Admin-only: write the company-wide volcengine-portrait key/AK/SK.
        Empty fields are silently ignored (treated as 'do not modify')."""
        if user.get("role") != "admin":
            self._json(403, {"ok": False, "error": "admin only"})
            return
        body = self._read_json()
        if body is None:
            return
        forwarded = {}
        for field in ("api_key", "access_key", "secret_key"):
            val = (body.get(field) or "").strip()
            if val:
                forwarded[field] = val
        if not forwarded:
            self._json(400, {"ok": False, "error": "no key field provided"})
            return
        try:
            port = APPS["volcengine-portrait"]["port"]
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            payload = json.dumps(forwarded).encode("utf-8")
            conn.request("POST", "/api/config", body=payload, headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
                "X-Is-Admin": "1",
            })
            resp = conn.getresponse()
            data = json.loads(resp.read()) if resp.status == 200 else {}
            status = resp.status
            conn.close()
        except Exception as exc:
            self._json(502, {"ok": False, "error": f"upstream error: {exc}"})
            return
        if status != 200:
            self._json(status, data or {"ok": False, "error": "upstream failed"})
            return
        self._json(200, {
            "ok": True,
            "has_api_key": bool(data.get("has_api_key")),
            "has_access_key": bool(data.get("has_access_key")),
            "has_secret_key": bool(data.get("has_secret_key")),
        })

    def _valid_report_date(self, date: str) -> bool:
        """Accept YYYY-MM-DD strictly, reject future dates. Emits 400 on failure."""
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
            self._json(400, {"ok": False, "error": "invalid date"})
            return False
        try:
            d = datetime.strptime(date, "%Y-%m-%d").date()
        except ValueError:
            self._json(400, {"ok": False, "error": "invalid date"})
            return False
        if d > datetime.now().date():
            self._json(400, {"ok": False, "error": "date in future"})
            return False
        return True

    def _report_csv_download(self, user: dict, date: str):
        if user.get("role") != "admin":
            self._json(403, {"ok": False, "error": "admin only"})
            return
        if not self._valid_report_date(date):
            return
        state_dir = STATE_DIR
        csv_path = state_dir / "reports" / f"{date}.csv"
        if not csv_path.exists():
            # generate on demand
            events, _ = _daily_report_module.load_events(state_dir, date)
            csv_path = _daily_report_module.write_csv(state_dir, date, events)
        data = csv_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="usage-{date}.csv"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _report_send(self, user: dict):
        if user.get("role") != "admin":
            self._json(403, {"ok": False, "error": "admin only"})
            return
        body = self._read_json() or {}
        date = str(body.get("date") or "").strip()
        if not self._valid_report_date(date):
            return
        cfg = _daily_report_module.load_config(STATE_DIR)
        key = _daily_report_module._load_deepseek_key(STATE_DIR)
        try:
            result = _daily_report_module.send_daily_report(STATE_DIR, date, cfg, deepseek_key=key, dry_run=False)
        except Exception as exc:
            self._json(500, {"ok": False, "error": f"send failed: {exc}"})
            return
        self._json(200, {"ok": result["ok"], "info": result["feishu_info"], "source": result["source"]})

    def _report_preview(self, user: dict):
        if user.get("role") != "admin":
            self._json(403, {"ok": False, "error": "admin only"})
            return
        body = self._read_json() or {}
        date = str(body.get("date") or "").strip()
        if not self._valid_report_date(date):
            return
        cfg = _daily_report_module.load_config(STATE_DIR)
        key = _daily_report_module._load_deepseek_key(STATE_DIR)
        try:
            result = _daily_report_module.send_daily_report(STATE_DIR, date, cfg, deepseek_key=key, dry_run=True)
        except Exception as exc:
            self._json(500, {"ok": False, "error": f"preview failed: {exc}"})
            return
        self._json(200, {
            "ok": True,
            "source": result["source"],
            "card": result["card"],
        })

    def _feishu_config_get(self, user: dict):
        if user.get("role") != "admin":
            self._json(403, {"ok": False, "error": "admin only"})
            return
        cfg = _daily_report_module.load_config(STATE_DIR)
        masked = dict(cfg)
        w = masked.get("webhook_url", "")
        if w:
            masked["webhook_url"] = w[:32] + "..." if len(w) > 35 else w
        s = masked.get("sign_secret", "")
        if s:
            masked["sign_secret_present"] = True
            masked["sign_secret"] = ""
        else:
            masked["sign_secret_present"] = False
        self._json(200, {"ok": True, "config": masked})

    def _feishu_config_put(self, user: dict):
        if user.get("role") != "admin":
            self._json(403, {"ok": False, "error": "admin only"})
            return
        body = self._read_json()
        if body is None:
            return
        updates: dict = {}
        for k in ("enabled", "webhook_url", "sign_secret", "schedule_time", "portal_base_url"):
            if k in body:
                updates[k] = body[k]
        cfg = _daily_report_module.save_config(STATE_DIR, updates)
        self._json(200, {"ok": True, "config": {k: cfg[k] for k in ("enabled", "schedule_time", "portal_base_url")}})

    def _platform_activity(self, user: dict):
        if not auth.has_permission(user, "view_stats_all"):
            self._json(200, {"ok": True, "activity": []})
            return
        merged = []
        for name, config in APPS.items():
            try:
                conn = http.client.HTTPConnection("127.0.0.1", config["port"], timeout=5)
                conn.request("GET", "/api/activity")
                resp = conn.getresponse()
                if resp.status == 200:
                    data = json.loads(resp.read())
                    items = (data.get("records")
                             or data.get("items")
                             or data.get("history")
                             or [])
                    for item in items[:20]:
                        item["_app"] = name
                        if not item.get("username"):
                            jid = item.get("job_id") or ""
                            item["username"] = tracker.get_job_owner(name, jid) if jid else ""
                        merged.append(item)
                conn.close()
            except Exception:
                pass
        merged.sort(key=lambda x: x.get("created_at") or x.get("time") or "", reverse=True)
        self._json(200, {"ok": True, "activity": merged[:50]})

    # ── Proxy ─────────────────────────────────────────────────────────────────

    def _try_proxy(self, path: str, method: str, user: dict) -> bool:
        for app_name, config in APPS.items():
            prefix = f"/{app_name}"
            if path == prefix or path.startswith(prefix + "/"):
                target_path = path[len(prefix):] or "/"
                # 透传 querystring（do_GET/do_POST/do_DELETE 提取 path 时
                # 用 urlparse 把 query 丢了，这里补回去 — 否则子应用拿不到
                # 像 ?group_ids=xxx 这种过滤参数）
                parsed = urllib.parse.urlparse(self.path)
                if parsed.query:
                    target_path = target_path + "?" + parsed.query
                if not auth.has_permission(user, "use_apps"):
                    self._json(403, {"ok": False, "error": "forbidden"})
                    return True
                self._proxy(app_name, config["port"], method, target_path, user)
                return True
        return False

    def _proxy(self, app_name: str, port: int, method: str, target_path: str, user: dict):
        client_ip = self.client_address[0]
        is_job = tracker._is_job_request(method, target_path)
        tracker.record(app_name, client_ip, method, target_path, username=user["username"])

        # Detect job type for usage stats. Video duration is read later from
        # /api/jobs/<id> directly, so we don't need to parse the request body.
        job_type = "image"
        if is_job:
            if app_name in ("seedance", "volcengine-portrait"):
                job_type = "video"
            elif app_name == "dreamina":
                # Mode is encoded in the path: /api/text2image, /api/frames2video, etc.
                job_type = "image" if any(x in target_path for x in ("text2image", "image2image")) else "video"

        try:
            body = None
            if method == "POST":
                length = int(self.headers.get("Content-Length") or "0")
                if length > 0:
                    body = self.rfile.read(length)

            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=300)
            headers: dict[str, str] = {}
            for key in ("Content-Type", "Content-Length", "Accept", "Accept-Encoding",
                        "X-Workspace-Id", "X-Api-Key", "X-Access-Key", "X-Secret-Key"):
                val = self.headers.get(key)
                if val:
                    headers[key] = val

            # Resolve stored key by ID if client sent X-Key-Id
            key_id = self.headers.get("X-Key-Id", "").strip()
            if key_id:
                key_val = key_manager.resolve(user["user_id"], key_id)
                if key_val:
                    if app_name == "volcengine-portrait" and ":::" in key_val:
                        ak, sk = key_val.split(":::", 1)
                        headers["X-Access-Key"] = ak
                        headers["X-Secret-Key"] = sk
                    else:
                        headers["X-Api-Key"] = key_val

            headers["X-Forwarded-For"] = client_ip
            # Propagate public-facing host/proto so subapps can build absolute URLs
            # for callbacks. cloudflared sets these when terminating the tunnel;
            # for direct LAN/HTTPS access we synthesize from the Host header.
            fwd_host = (self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or "").strip()
            if fwd_host:
                headers["X-Forwarded-Host"] = fwd_host
            fwd_proto = (self.headers.get("X-Forwarded-Proto") or "").strip()
            if not fwd_proto:
                fwd_proto = "https" if self._is_https() else "http"
            headers["X-Forwarded-Proto"] = fwd_proto
            # Propagate the logged-in username to every sub-app for per-user
            # job ownership / filtering. X-Is-Admin is now strictly tied to the
            # admin role; the dreamina-specific "everyone can manage accounts"
            # affordance moves to X-Dreamina-Manage so task views can be
            # per-user even when account management is open to all.
            # http.client headers must be latin-1 safe, so URL-percent-encode
            # the username — sub-apps urllib.parse.unquote it back. Empty/ASCII
            # names pass through unchanged.
            headers["X-Username"] = urllib.parse.quote(user.get("username", ""), safe="")
            if user.get("role") == "admin" or auth.has_permission(user, "manage_dreamina_accounts"):
                headers["X-Is-Admin"] = "1"
            if app_name == "dreamina" and auth.has_permission(user, "use_apps"):
                headers["X-Dreamina-Manage"] = "1"

            conn.request(method, target_path, body=body, headers=headers)
            resp = conn.getresponse()

            # job_id is surfaced by the sub-app via the X-Job-Id response header
            # so we can register usage stats without buffering the body. This is
            # the P0 fix for #15: long-running creation responses no longer hold
            # the proxy thread waiting for resp.read() to complete.
            if is_job and resp.status in (200, 201):
                jid_header = resp.getheader("X-Job-Id", "").strip()
                if jid_header:
                    tracker.register_job(app_name, jid_header, user["username"], job_type)
                    tracker.inc_daily_jobs(app_name)

            content_type = resp.getheader("Content-Type", "")
            # Only buffer JSON bodies we still need to inspect (none for now —
            # X-Job-Id covers the historic reason). Keep `is_job` out of the
            # condition so creation responses stream like everything else.
            should_buffer = False

            if should_buffer:
                resp_body = resp.read()
                self.send_response(resp.status)
                for key, value in resp.getheaders():
                    if key.lower() in ("transfer-encoding", "connection", "server", "date", "content-length"):
                        continue
                    self.send_header(key, value)
                if target_path.endswith((".html", ".js", ".css", ".mjs")):
                    self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self._cors_headers()
                self.send_header("Content-Length", str(len(resp_body)))
                self.end_headers()
                self.wfile.write(resp_body)
            else:
                # Stream everything (job creation, media, JSON status) without buffering
                self.send_response(resp.status)
                for key, value in resp.getheaders():
                    if key.lower() in ("transfer-encoding", "connection", "server", "date"):
                        continue
                    self.send_header(key, value)
                if target_path.endswith((".html", ".js", ".css", ".mjs")):
                    self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self._cors_headers()
                self.end_headers()
                shutil.copyfileobj(resp, self.wfile, length=65536)

            conn.close()
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
            pass
        except Exception as exc:
            msg = str(exc)[:300]
            try:
                self._json(502, {"ok": False, "error": f"proxy error: {msg}"})
            except (BrokenPipeError, ConnectionResetError, ssl.SSLError, OSError):
                pass

    # ── Static file serving ───────────────────────────────────────────────────

    def _serve_portal(self, path: str):
        if path in ("/", ""):
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
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self._cors_headers()
        self.end_headers()
        self.wfile.write(content)

    # ── Shared helpers ────────────────────────────────────────────────────────

    def _json(self, status: int, data: dict):
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self._cors_headers()
        self.end_headers()
        self.wfile.write(raw)

    def _redirect(self, url: str):
        self.send_response(302)
        self.send_header("Location", url)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _internal_finalize_job(self):
        """Loopback-only endpoint sub-apps call to declare a job's terminal
        status. Failures roll back the +1 inc_daily_jobs applied at register
        time. Authenticated by a shared random token injected into the
        sub-app's env at portal startup."""
        token = self.headers.get("X-Internal-Token", "")
        client_ip = self.client_address[0]
        if token != INTERNAL_TOKEN or not client_ip.startswith("127."):
            self._json(403, {"ok": False, "error": "forbidden"})
            return
        body = self._read_json()
        if body is None:
            return
        app = (body.get("app") or "").strip()
        job_id = (body.get("job_id") or "").strip()
        status = (body.get("status") or "").strip()
        if not app or not job_id or not status:
            self._json(400, {"ok": False, "error": "missing fields"})
            return
        rolled = tracker.finalize_job(app, job_id, status)
        self._json(200, {"ok": True, "rolled_back": rolled})

    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin") or "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, X-Workspace-Id, X-Api-Key, X-Access-Key, X-Secret-Key, X-Key-Id")
        self.send_header("Access-Control-Allow-Credentials", "true")

    def _read_json(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length") or "0")
            raw = self.rfile.read(length) if length > 0 else b"{}"
            return json.loads(raw)
        except Exception:
            self._json(400, {"ok": False, "error": "invalid JSON"})
            return None


# ─── Main ──────────────────────────────────────────────────────────────────────

def main():
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    lan_ip = get_lan_ip()
    # PORTAL_HTTP_ONLY=1 forces HTTP mode (TLS terminates at upstream proxy like Cloudflare Tunnel)
    if os.environ.get("PORTAL_HTTP_ONLY") == "1":
        certs = None
        print("  [INFO] PORTAL_HTTP_ONLY=1 — running HTTP-only (TLS handled upstream)")
    else:
        certs = ensure_certs(_DATA_BASE / "certs")

    # Bump listen backlog so bursts of concurrent requests during job submission
    # storms aren't dropped at the kernel layer (default is 5 — too tight for the
    # 10+ users we now have). Set the class attribute before instantiation so
    # server_activate() picks it up. 64 keeps memory cost negligible.
    ThreadingHTTPServer.request_queue_size = 64
    server = ThreadingHTTPServer(("0.0.0.0", PORTAL_PORT), Handler)
    redirect_server = None

    if certs:
        cert_file, key_file = certs
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(cert_file), str(key_file))
        server.socket = ctx.wrap_socket(server.socket, server_side=True)

        class RedirectHandler(SimpleHTTPRequestHandler):
            def log_message(self, format, *args): pass
            def do_GET(self):
                host = self.headers.get("Host", "").split(":")[0] or lan_ip
                https_url = f"https://{host}:{PORTAL_PORT}{self.path}"
                page = (f'<script>var h=window.location.hostname;'
                        f'window.location.replace("https://"+h+":{PORTAL_PORT}"+window.location.pathname+window.location.search);</script>'
                        f'<a href="{https_url}">{https_url}</a>').encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(page)
            do_POST = do_GET
            do_OPTIONS = do_GET

        redirect_server = ThreadingHTTPServer(("0.0.0.0", REDIRECT_PORT), RedirectHandler)
        print(f"\n  AI Generation Portal (HTTPS):")
        print(f"    Local:   https://127.0.0.1:{PORTAL_PORT}")
        print(f"    LAN:     https://{lan_ip}:{PORTAL_PORT}")
    else:
        print(f"\n  AI Generation Portal (HTTP):")
        print(f"    Local:   http://127.0.0.1:{PORTAL_PORT}")
        print(f"    LAN:     http://{lan_ip}:{PORTAL_PORT}")

    print("Starting sub-applications...")
    manager.start_all()
    time.sleep(2)
    if redirect_server:
        threading.Thread(target=redirect_server.serve_forever, daemon=True).start()

    # Feishu daily report scheduler (daemon, tolerates all errors internally)
    threading.Thread(
        target=_daily_report_module.scheduler_loop,
        args=(STATE_DIR,),
        daemon=True,
        name="daily_report_scheduler",
    ).start()
    print("  [daily_report] scheduler thread started", flush=True)

    for name, config in APPS.items():
        print(f"    {name:20s} -> http://127.0.0.1:{config['port']}")

    if auth.first_run():
        print(f"\n  [SETUP] No users found — visit the portal to create the admin account")
    print(f"\n  Press Ctrl+C to stop\n")

    def shutdown_handler(sig, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, shutdown_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, shutdown_handler)
    elif hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, shutdown_handler)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        manager.shutdown()
        if redirect_server:
            redirect_server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
