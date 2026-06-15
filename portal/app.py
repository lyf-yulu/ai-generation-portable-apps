#!/usr/bin/env python3
from __future__ import annotations

import http.client
import json
import mimetypes
import os
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"

# Windows: suppress console windows for spawned subprocesses
_POPEN_EXTRA: dict[str, Any] = {}
if hasattr(subprocess, "CREATE_NO_WINDOW"):
    _POPEN_EXTRA["creationflags"] = subprocess.CREATE_NO_WINDOW
STATE_DIR = ROOT / "state"
USAGE_PATH = STATE_DIR / "usage.json"

APPS = {
    "seedance": {"dir": ROOT.parent / "seedance", "port": 8787},
    "nano-banana": {"dir": ROOT.parent / "nano-banana", "port": 8797},
    "dreamina": {"dir": ROOT.parent / "dreamina", "port": 8888},
}

PORTAL_PORT = 9090
REDIRECT_PORT = 9089  # HTTP → HTTPS redirect port


def get_lan_ip() -> str:
    try:
        if sys.platform == "win32":
            out = subprocess.check_output(["ipconfig"], text=True, stderr=subprocess.DEVNULL, encoding="gbk", errors="replace")
            for line in out.splitlines():
                line = line.strip()
                if "IPv4" in line and "127.0.0.1" not in line:
                    ip = line.rsplit(":", 1)[-1].strip()
                    if ip.startswith("192.168.") or ip.startswith("10.") or ip.startswith("172."):
                        return ip
        else:
            out = subprocess.check_output(["ifconfig"], text=True, stderr=subprocess.DEVNULL)
            for line in out.splitlines():
                if "inet " in line and "127.0.0.1" not in line:
                    ip = line.strip().split()[1]
                    if ip.startswith("192.168.") or ip.startswith("10.") or ip.startswith("172."):
                        return ip
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _find_openssl() -> str | None:
    """Find openssl binary. Returns path or None."""
    import shutil
    which = shutil.which("openssl")
    if which:
        return which
    # Windows: check common Git/OpenSSL install paths
    if sys.platform == "win32":
        candidates = [
            r"C:\Program Files\Git\usr\bin\openssl.exe",
            r"C:\Program Files\OpenSSL\bin\openssl.exe",
            r"C:\Program Files (x86)\Git\usr\bin\openssl.exe",
        ]
        for p in candidates:
            if Path(p).exists():
                return p
    return None


def ensure_certs(cert_dir: Path) -> tuple[Path, Path] | None:
    cert_file = cert_dir / "cert.pem"
    key_file = cert_dir / "key.pem"
    if cert_file.exists() and key_file.exists():
        return cert_file, key_file

    openssl = _find_openssl()
    if not openssl:
        print("  [WARN] openssl not found — running in HTTP-only mode")
        print("  Install OpenSSL (or Git for Windows) to enable HTTPS")
        return None

    cert_dir.mkdir(parents=True, exist_ok=True)
    lan_ip = get_lan_ip()
    subprocess.run([
        openssl, "req", "-x509", "-newkey", "rsa:2048",
        "-keyout", str(key_file), "-out", str(cert_file),
        "-days", "365", "-nodes",
        "-subj", "/CN=AI Generation Portal",
        "-addext", f"subjectAltName=DNS:localhost,IP:127.0.0.1,IP:{lan_ip}"
    ], check=True, capture_output=True)
    print(f"  Generated self-signed certificate (LAN IP: {lan_ip})")
    return cert_file, key_file


class AppManager:
    def __init__(self):
        self.processes: dict[str, subprocess.Popen] = {}
        self.log_handles: dict[str, Any] = {}
        self.status: dict[str, dict[str, Any]] = {}
        self._stop_event = threading.Event()

    def start_all(self):
        for name, config in APPS.items():
            self.start_app(name, config)
        threading.Thread(target=self._health_loop, daemon=True).start()

    def start_app(self, name: str, config: dict):
        app_dir = config["dir"]
        if not (app_dir / "app.py").exists():
            self.status[name] = {"status": "missing", "error": "app.py not found"}
            return
        env = os.environ.copy()
        env["PORT"] = str(config["port"])
        env["HOST"] = "127.0.0.1"
        env["CORS"] = "1"
        old_log = self.log_handles.pop(name, None)
        if old_log:
            try:
                old_log.close()
            except Exception:
                pass
        log_dir = STATE_DIR / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = (log_dir / f"{name}.log").open("ab", buffering=0)
        try:
            proc = subprocess.Popen(
                [sys.executable, "app.py"],
                cwd=str(app_dir),
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                **_POPEN_EXTRA,
            )
        except Exception as exc:
            log_file.close()
            self.status[name] = {"status": "crashed", "error": str(exc), "port": config["port"]}
            return
        self.log_handles[name] = log_file
        self.processes[name] = proc
        self.status[name] = {"status": "starting", "port": config["port"], "pid": proc.pid}

    def _health_loop(self):
        while not self._stop_event.is_set():
            time.sleep(10)
            for name, config in APPS.items():
                proc = self.processes.get(name)
                if proc and proc.poll() is not None:
                    self.status[name] = {"status": "crashed", "exit_code": proc.returncode}
                    self.start_app(name, config)
                    continue
                try:
                    conn = http.client.HTTPConnection("127.0.0.1", config["port"], timeout=5)
                    conn.request("GET", "/api/v1/meta")
                    resp = conn.getresponse()
                    if resp.status == 200:
                        meta = json.loads(resp.read())
                        self.status[name] = {"status": "ready", "port": config["port"], "meta": meta}
                    else:
                        self.status[name] = {"status": "unhealthy", "port": config["port"]}
                    conn.close()
                except Exception:
                    if proc and proc.poll() is None:
                        self.status[name] = {"status": "starting", "port": config["port"]}
                    else:
                        self.status[name] = {"status": "down", "port": config["port"]}

    def shutdown(self):
        self._stop_event.set()
        for name, proc in self.processes.items():
            if proc.poll() is None:
                proc.terminate()
        for name, proc in self.processes.items():
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        for handle in self.log_handles.values():
            try:
                handle.close()
            except Exception:
                pass
        self.log_handles.clear()


class UsageTracker:
    def __init__(self):
        self._lock = threading.Lock()
        self._data = self._load()
        self._pending_jobs: list[dict] = []
        threading.Thread(target=self._job_poll_loop, daemon=True).start()

    def _load(self) -> dict[str, Any]:
        if USAGE_PATH.exists():
            try:
                return json.loads(USAGE_PATH.read_text("utf-8"))
            except Exception:
                pass
        return {"records": [], "daily": {}}

    def _save(self):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        USAGE_PATH.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), "utf-8")

    def record(self, app: str, client_ip: str, method: str, path: str):
        today = time.strftime("%Y-%m-%d")
        entry = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "app": app, "ip": client_ip, "method": method, "path": path}
        with self._lock:
            self._data["records"].append(entry)
            if len(self._data["records"]) > 5000:
                self._data["records"] = self._data["records"][-3000:]
            day_stats = self._data["daily"].setdefault(today, {})
            app_stats = day_stats.setdefault(app, {"requests": 0, "jobs": 0})
            app_stats["requests"] += 1
            # Jobs are counted later by register_job → _add_ip_jobs when the
            # task reaches a terminal status.  Do NOT increment jobs here or
            # each submission will be counted twice (once on submit + once on
            # completion with the actual total).
            self._save()

    def register_job(self, app: str, job_id: str, client_ip: str):
        with self._lock:
            self._pending_jobs.append({
                "app": app, "job_id": job_id, "ip": client_ip,
                "submitted_at": time.time(), "date": time.strftime("%Y-%m-%d")
            })

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
                        status = data.get("status", "")
                        terminal = status in ("succeeded", "failed", "completed")
                        if terminal:
                            # Count actual completed sub-tasks, not requested total
                            actual = max(1, int(data.get("done") or 0))
                            self._add_ip_jobs(job["date"], job["ip"], job["app"], actual)
                            done_ids.append(job["job_id"])
                    conn.close()
                except Exception:
                    pass
                if time.time() - job["submitted_at"] > 7200:
                    done_ids.append(job["job_id"])
            if done_ids:
                with self._lock:
                    self._pending_jobs = [j for j in self._pending_jobs if j["job_id"] not in done_ids]

    def _add_ip_jobs(self, date: str, ip: str, app: str, count: int):
        with self._lock:
            by_ip = self._data.setdefault("by_ip", {}).setdefault(date, {})
            ip_stats = by_ip.setdefault(ip, {})
            ip_stats[app] = ip_stats.get(app, 0) + count
            self._save()

    def _is_job_request(self, method: str, path: str) -> bool:
        if method != "POST":
            return False
        job_patterns = ["/api/jobs", "/api/text2image", "/api/image2image", "/api/text2video",
                        "/api/image2video", "/api/frames2video", "/api/multimodal2video", "/api/multiframe2video"]
        return any(path.startswith(p) for p in job_patterns)

    def get_stats(self) -> dict[str, Any]:
        today = time.strftime("%Y-%m-%d")
        with self._lock:
            today_stats = self._data["daily"].get(today, {})
            total_jobs = sum(v.get("jobs", 0) for v in today_stats.values())
            total_requests = sum(v.get("requests", 0) for v in today_stats.values())
            recent = self._data["records"][-20:]
            by_ip_today = self._data.get("by_ip", {}).get(today, {})
        return {
            "today": today,
            "today_jobs": total_jobs,
            "today_requests": total_requests,
            "by_app": today_stats,
            "by_ip": by_ip_today,
            "recent": recent,
            "daily": dict(list(self._data["daily"].items())[-7:]),
        }


manager = AppManager()
tracker = UsageTracker()


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path

        if path == "/api/platform/status":
            self._platform_status()
        elif path == "/api/platform/stats":
            self._platform_stats()
        elif path == "/api/platform/activity":
            self._platform_activity()
        elif self._try_proxy(path, "GET"):
            pass
        else:
            self._serve_portal(path)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if not self._try_proxy(path, "POST"):
            self._json(404, {"ok": False, "error": "not found"})

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _platform_status(self):
        lan_ip = get_lan_ip()
        apps_info = []
        for name, config in APPS.items():
            info = {"name": name, "port": config["port"], **manager.status.get(name, {"status": "unknown"})}
            info["url"] = f"/{name}/"
            apps_info.append(info)
        self._json(200, {"ok": True, "lan_ip": lan_ip, "portal_port": PORTAL_PORT, "apps": apps_info})

    def _platform_stats(self):
        self._json(200, {"ok": True, **tracker.get_stats()})

    def _platform_activity(self):
        merged = []
        for name, config in APPS.items():
            try:
                conn = http.client.HTTPConnection("127.0.0.1", config["port"], timeout=5)
                conn.request("GET", "/api/activity")
                resp = conn.getresponse()
                if resp.status == 200:
                    data = json.loads(resp.read())
                    items = data.get("items") or data.get("history") or []
                    for item in items[:20]:
                        item["_app"] = name
                        merged.append(item)
                conn.close()
            except Exception:
                pass
        merged.sort(key=lambda x: x.get("created_at") or x.get("time") or "", reverse=True)
        self._json(200, {"ok": True, "activity": merged[:50]})

    def _try_proxy(self, path: str, method: str) -> bool:
        for app_name, config in APPS.items():
            prefix = f"/{app_name}"
            if path == prefix or path.startswith(prefix + "/"):
                target_path = path[len(prefix):] or "/"
                self._proxy(app_name, config["port"], method, target_path)
                return True
        return False

    def _proxy(self, app_name: str, port: int, method: str, target_path: str):
        # NEVER trust client-provided X-Forwarded-For — use real TCP address.
        # Portal is the edge; sub-apps trust X-Forwarded-For from Portal only.
        client_ip = self.client_address[0]
        is_job = tracker._is_job_request(method, target_path)
        tracker.record(app_name, client_ip, method, target_path)

        try:
            body = None
            if method == "POST":
                length = int(self.headers.get("Content-Length") or "0")
                if length > 0:
                    body = self.rfile.read(length)

            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=300)
            headers = {}
            for key in ("Content-Type", "Content-Length", "Accept", "Accept-Encoding", "X-Workspace-Id"):
                val = self.headers.get(key)
                if val:
                    headers[key] = val
            headers["X-Forwarded-For"] = client_ip

            conn.request(method, target_path, body=body, headers=headers)
            resp = conn.getresponse()

            resp_body = resp.read()

            if is_job and resp.status in (200, 201):
                try:
                    resp_data = json.loads(resp_body)
                    job_id = resp_data.get("job_id") or resp_data.get("id")
                    if job_id:
                        tracker.register_job(app_name, str(job_id), client_ip)
                except Exception:
                    pass

            self.send_response(resp.status)
            for key, value in resp.getheaders():
                if key.lower() in ("transfer-encoding", "connection"):
                    continue
                self.send_header(key, value)
            self._cors_headers()
            self.send_header("Content-Length", str(len(resp_body)))
            self.end_headers()
            self.wfile.write(resp_body)
            conn.close()
        except Exception as exc:
            # Truncate to avoid leaking backend response bodies (may contain API keys)
            msg = str(exc)
            if len(msg) > 300:
                msg = msg[:300] + "..."
            self._json(502, {"ok": False, "error": f"proxy error: {msg}"})

    def _serve_portal(self, path: str):
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
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self._cors_headers()
        self.end_headers()
        self.wfile.write(content)

    def _json(self, status: int, data: dict):
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self._cors_headers()
        self.end_headers()
        self.wfile.write(raw)

    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")


def main():
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    lan_ip = get_lan_ip()
    certs = ensure_certs(ROOT / "certs")

    server = ThreadingHTTPServer(("0.0.0.0", PORTAL_PORT), Handler)
    redirect_server = None

    if certs:
        cert_file, key_file = certs
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(cert_file), str(key_file))
        server.socket = ctx.wrap_socket(server.socket, server_side=True)

        class RedirectHandler(SimpleHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_GET(self):
                host = self.headers.get("Host", "").split(":")[0] or lan_ip
                self.send_response(301)
                self.send_header("Location", f"https://{host}:{PORTAL_PORT}{self.path}")
                self.send_header("Connection", "close")
                self.end_headers()

            do_POST = do_GET
            do_OPTIONS = do_GET

        redirect_server = ThreadingHTTPServer(("0.0.0.0", REDIRECT_PORT), RedirectHandler)

        print(f"\n  AI Generation Portal running (HTTPS):")
        print(f"    Local:   https://127.0.0.1:{PORTAL_PORT}")
        print(f"    LAN:     https://{lan_ip}:{PORTAL_PORT}")
        print(f"    HTTP redirect: http://{lan_ip}:{REDIRECT_PORT} → https://{lan_ip}:{PORTAL_PORT}")
        print(f"\n  Note: First visit requires accepting the self-signed certificate")
    else:
        print(f"\n  AI Generation Portal running (HTTP-only):")
        print(f"    Local:   http://127.0.0.1:{PORTAL_PORT}")
        print(f"    LAN:     http://{lan_ip}:{PORTAL_PORT}")
        print(f"\n  Note: Install openssl to enable HTTPS (e.g. Git for Windows)")
        print(f"    File System Access API (select directory) will not work without HTTPS.")

    print("Starting sub-applications...")
    manager.start_all()
    time.sleep(2)
    if redirect_server:
        threading.Thread(target=redirect_server.serve_forever, daemon=True).start()

    print(f"\n  Sub-apps:")
    for name, config in APPS.items():
        print(f"    {name:14s} -> http://127.0.0.1:{config['port']}")
    print(f"  Press Ctrl+C to stop all services\n")

    def shutdown_handler(sig, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, shutdown_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, shutdown_handler)
    # Windows: SIGBREAK is the closest equivalent to SIGTERM
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
