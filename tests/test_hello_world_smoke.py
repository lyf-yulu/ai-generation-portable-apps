"""Hello-World subapp smoke test.

Verifies the reference sub-app implementation meets the portal contract:
- /api/v1/meta returns expected shape
- POST /api/jobs returns X-Job-Id header (portal reads this to track usage)
- job transitions pending -> succeeded within ~2s
- GET /api/jobs/<id> returns the echoed message

Runs hello-world/app.py in a subprocess on an ephemeral port; no portal
required. Kills the subprocess in tearDown.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import unittest
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HELLO = ROOT / "hello-world" / "app.py"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _http_get(url: str, timeout: float = 3.0) -> tuple[int, bytes, dict]:
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def _http_post_json(url: str, body: dict, timeout: float = 3.0) -> tuple[int, bytes, dict]:
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


class HelloWorldSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = _free_port()
        env = os.environ.copy()
        env["PORT"] = str(cls.port)
        env["HOST"] = "127.0.0.1"
        env["CORS"] = "0"
        cls.proc = subprocess.Popen(
            [sys.executable, str(HELLO)],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        # Wait for it to bind
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                s = socket.socket()
                s.settimeout(0.5)
                s.connect(("127.0.0.1", cls.port))
                s.close()
                break
            except OSError:
                time.sleep(0.1)
        else:
            cls.proc.kill()
            raise RuntimeError("hello-world did not bind in 5s")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            cls.proc.kill()

    def _url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def test_meta_endpoint(self):
        status, body, _ = _http_get(self._url("/api/v1/meta"))
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["app"], "hello-world")
        self.assertIn("echo", data["capabilities"])
        self.assertEqual(data["status"], "ready")

    def test_config_endpoint(self):
        status, body, _ = _http_get(self._url("/api/config"))
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        self.assertFalse(data["has_key"])

    def test_job_creation_returns_x_job_id_header(self):
        status, body, headers = _http_post_json(self._url("/api/jobs"), {"message": "hi"})
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        # Header name is case-insensitive in HTTP; urllib gives Title-Case
        header_names = {k.lower() for k in headers}
        self.assertIn("x-job-id", header_names, f"missing X-Job-Id in {headers}")
        # Header value must equal body job_id
        job_id_hdr = next(v for k, v in headers.items() if k.lower() == "x-job-id")
        self.assertEqual(job_id_hdr, data["job_id"])

    def test_job_status_transitions_to_succeeded(self):
        _, body, _ = _http_post_json(self._url("/api/jobs"), {"message": "hello test"})
        job_id = json.loads(body)["job_id"]
        # Initially pending; give the 1s background thread time to finish.
        deadline = time.time() + 4
        status_value = None
        while time.time() < deadline:
            _, jbody, _ = _http_get(self._url(f"/api/jobs/{job_id}"))
            jdata = json.loads(jbody)
            status_value = jdata["status"]
            if status_value == "succeeded":
                self.assertEqual(jdata["result"]["echo"], "hello test")
                self.assertEqual(jdata["done"], 1)
                return
            time.sleep(0.2)
        self.fail(f"job did not complete; last status={status_value}")

    def test_missing_message_rejected(self):
        status, body, _ = _http_post_json(self._url("/api/jobs"), {})
        self.assertEqual(status, 400)
        self.assertIn("message required", json.loads(body)["error"])

    def test_job_list_endpoint(self):
        _http_post_json(self._url("/api/jobs"), {"message": "list-1"})
        _http_post_json(self._url("/api/jobs"), {"message": "list-2"})
        status, body, _ = _http_get(self._url("/api/jobs"))
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        self.assertIsInstance(data["jobs"], list)

    def test_activity_endpoint(self):
        status, body, _ = _http_get(self._url("/api/activity"))
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertIn("records", data)


if __name__ == "__main__":
    unittest.main()
