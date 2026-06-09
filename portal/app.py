#!/usr/bin/env python3
from __future__ import annotations

import http.client
import json
import mimetypes
import os
import signal
import socket
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
STATE_DIR = ROOT / "state"
USAGE_PATH = STATE_DIR / "usage.json"

APPS = {
    "seedance": {"dir": ROOT.parent / "seedance", "port": 8787},
    "nano-banana": {"dir": ROOT.parent / "nano-banana", "port": 8797},
    "dreamina": {"dir": ROOT.parent / "dreamina", "port": 8888},
}

PORTAL_PORT = 8080


def get_lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


class AppManager:
    def __init__(self):
        self.processes: dict[str, subprocess.Popen] = {}
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
        proc = subprocess.Popen(
            [sys.executable, "app.py"],
            cwd=str(app_dir),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
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


class UsageTracker:
    def __init__(self):
        self._lock = threading.Lock()
        self._data = self._load()

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
            if self._is_job_request(method, path):
                app_stats["jobs"] += 1
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
        return {
            "today": today,
            "today_jobs": total_jobs,
            "today_requests": total_requests,
            "by_app": today_stats,
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
        client_ip = self.client_address[0]
        tracker.record(app_name, client_ip, method, target_path)

        try:
            body = None
            if method == "POST":
                length = int(self.headers.get("Content-Length") or "0")
                if length > 0:
                    body = self.rfile.read(length)

            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=300)
            headers = {}
            for key in ("Content-Type", "Content-Length", "Accept", "Accept-Encoding"):
                val = self.headers.get(key)
                if val:
                    headers[key] = val

            conn.request(method, target_path, body=body, headers=headers)
            resp = conn.getresponse()

            self.send_response(resp.status)
            for key, value in resp.getheaders():
                if key.lower() in ("transfer-encoding", "connection"):
                    continue
                self.send_header(key, value)
            self._cors_headers()
            self.end_headers()

            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
            conn.close()
        except Exception as e:
            self._json(502, {"ok": False, "error": f"proxy error: {e}"})

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

    print("Starting sub-applications...")
    manager.start_all()
    time.sleep(2)

    lan_ip = get_lan_ip()
    server = ThreadingHTTPServer(("0.0.0.0", PORTAL_PORT), Handler)

    print(f"\n  AI Generation Portal running:")
    print(f"    Local:   http://127.0.0.1:{PORTAL_PORT}")
    print(f"    LAN:     http://{lan_ip}:{PORTAL_PORT}")
    print(f"\n  Sub-apps:")
    for name, config in APPS.items():
        print(f"    {name:14s} -> http://127.0.0.1:{config['port']}")
    print(f"\n  Press Ctrl+C to stop all services\n")

    def shutdown_handler(sig, frame):
        print("\nShutting down...")
        manager.shutdown()
        server.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        manager.shutdown()


if __name__ == "__main__":
    main()
