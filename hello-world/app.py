#!/usr/bin/env python3
"""Hello-World echo subapp — the minimum viable sub-app.

Serves as the reference implementation of the portal sub-app contract:
- reads PORT / HOST / CORS / DATA_DIR / PORTAL_INTERNAL_TOKEN from env
- exposes /api/v1/meta, /api/config, /api/jobs, /api/jobs/<id>, /api/activity
- returns X-Job-Id on job-creation responses (portal needs this to track usage)
- calls back to portal /api/internal/jobs/finalize when a job reaches a terminal state
- verifies X-Portal-Sig HMAC before trusting X-Is-Admin

A "job" here just echoes back the submitted `message` after a 1s delay. Enough
to exercise the entire portal proxy path (auth, HMAC sig, usage tracker,
frontend polling) end-to-end.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import ssl
import threading
import time
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", str(ROOT)))
STATE_DIR = DATA_DIR / "state"
STATE_DIR.mkdir(parents=True, exist_ok=True)

PORT = int(os.environ.get("PORT", "8899"))
HOST = os.environ.get("HOST", "127.0.0.1")
CORS = os.environ.get("CORS", "").strip() in ("1", "true", "yes")
PORTAL_PORT = int(os.environ.get("PORTAL_PORT", "9090"))
PORTAL_INTERNAL_TOKEN = os.environ.get("PORTAL_INTERNAL_TOKEN", "")
PORTAL_SIG_WINDOW = int(os.environ.get("PORTAL_SIG_WINDOW", "60"))

# In-memory job registry. Not persisted — this app is a demo/reference.
_JOBS: dict[str, dict[str, Any]] = {}
_ACTIVITY: list[dict[str, Any]] = []
_LOCK = threading.Lock()


def _verify_portal_sig(handler: BaseHTTPRequestHandler) -> bool:
    if not PORTAL_INTERNAL_TOKEN:
        return False
    ts = handler.headers.get("X-Portal-Ts", "").strip()
    sig = handler.headers.get("X-Portal-Sig", "").strip()
    if not ts or not sig:
        return False
    try:
        if abs(int(time.time()) - int(ts)) > PORTAL_SIG_WINDOW:
            return False
    except ValueError:
        return False
    is_admin_flag = "1" if handler.headers.get("X-Is-Admin") == "1" else "0"
    username = handler.headers.get("X-Username", "")
    msg = f"{ts}:{is_admin_flag}:{username}".encode("utf-8")
    expected = hmac.new(
        PORTAL_INTERNAL_TOKEN.encode("utf-8"), msg, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(sig, expected)


def _decode_username(handler: BaseHTTPRequestHandler) -> str:
    return urllib.parse.unquote(handler.headers.get("X-Username", ""))


def _report_final_to_portal(job_id: str, status: str) -> None:
    if not PORTAL_INTERNAL_TOKEN or not job_id:
        return
    try:
        payload = json.dumps({"job_id": job_id, "status": status}).encode("utf-8")
        req = urllib.request.Request(
            f"https://127.0.0.1:{PORTAL_PORT}/api/internal/jobs/finalize",
            data=payload,
            headers={
                "X-Internal-Token": PORTAL_INTERNAL_TOKEN,
                "Content-Type": "application/json",
            },
        )
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        urllib.request.urlopen(req, timeout=5, context=ctx).read()
    except Exception:
        # Portal will still see the job via poll; finalize is best-effort.
        pass


def _run_job(job_id: str) -> None:
    time.sleep(1)  # simulate work
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return
        job["status"] = "succeeded"
        job["done"] = 1
        job["finished_at"] = time.time()
        job["result"] = {"echo": job.get("message", "")}
    _report_final_to_portal(job_id, "succeeded")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Reduce log noise; startup + errors still go to stderr via log_error.
        pass

    def _cors(self):
        if CORS:
            origin = self.headers.get("Origin", "*")
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Credentials", "true")
            self.send_header("Access-Control-Expose-Headers", "X-Job-Id")

    def _json(self, status: int, payload: dict, extra_headers: dict[str, str] | None = None):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length") or "0")
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            return json.loads(raw.decode("utf-8"))
        except Exception:
            self._json(400, {"ok": False, "error": "invalid json body"})
            return None

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, X-Api-Key, X-Workspace-Id, X-Username, "
                         "X-Is-Admin, X-Portal-Ts, X-Portal-Sig")
        self._cors()
        self.end_headers()

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/v1/meta":
            self._json(200, {"app": "hello-world", "version": "1.0.0", "port": PORT,
                             "capabilities": ["echo"], "status": "ready"})
        elif path == "/api/config":
            self._json(200, {"ok": True, "has_key": False})
        elif path == "/api/jobs":
            with _LOCK:
                username = _decode_username(self)
                is_admin = self.headers.get("X-Is-Admin") == "1" and _verify_portal_sig(self)
                jobs = list(_JOBS.values()) if is_admin else [
                    j for j in _JOBS.values() if j.get("username") == username
                ]
                jobs.sort(key=lambda j: j.get("created_at", 0), reverse=True)
            self._json(200, {"ok": True, "jobs": jobs[:50]})
        elif path.startswith("/api/jobs/"):
            job_id = path[len("/api/jobs/"):]
            with _LOCK:
                job = _JOBS.get(job_id)
            if not job:
                self._json(404, {"ok": False, "error": "not found"})
                return
            self._json(200, job)
        elif path == "/api/activity":
            with _LOCK:
                self._json(200, {"ok": True, "records": list(_ACTIVITY[-20:])})
        elif path == "/" or path == "/index.html":
            self._serve_file(ROOT / "static" / "index.html", "text/html")
        else:
            static_path = ROOT / "static" / path.lstrip("/")
            if static_path.is_file() and static_path.resolve().is_relative_to((ROOT / "static").resolve()):
                self._serve_file(static_path, self._guess_type(static_path))
            else:
                self._json(404, {"ok": False, "error": "not found"})

    def _guess_type(self, p: Path) -> str:
        ext = p.suffix.lower()
        return {".html": "text/html", ".js": "application/javascript",
                ".css": "text/css", ".json": "application/json"}.get(ext, "text/plain")

    def _serve_file(self, path: Path, ctype: str):
        try:
            body = path.read_bytes()
        except OSError:
            self._json(404, {"ok": False, "error": "not found"})
            return
        self.send_response(200)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path != "/api/jobs":
            self._json(404, {"ok": False, "error": "not found"})
            return
        body = self._read_json()
        if body is None:
            return
        message = str(body.get("message") or "").strip()
        if not message:
            self._json(400, {"ok": False, "error": "message required"})
            return
        job_id = str(uuid.uuid4())
        username = _decode_username(self)
        workspace_id = (self.headers.get("X-Workspace-Id") or "default").strip()
        with _LOCK:
            _JOBS[job_id] = {
                "id": job_id,
                "job_id": job_id,
                "status": "pending",
                "done": 0,
                "message": message,
                "username": username,
                "workspace_id": workspace_id,
                "created_at": time.time(),
                "task_type": "echo",
            }
            _ACTIVITY.append({"job_id": job_id, "username": username, "created_at": time.time()})
        threading.Thread(target=_run_job, args=(job_id,), daemon=True).start()
        # X-Job-Id lets portal register the job for usage tracking without
        # parsing the full body — see portal/app.py:1828 (Content-Disposition
        # comment) for why.
        self._json(200, {"ok": True, "job_id": job_id}, extra_headers={"X-Job-Id": job_id})


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"hello-world echo listening on http://{HOST}:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
