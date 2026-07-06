"""Verify /api/jobs list responses expose workspace_id for both sub-apps.

The frontend in-app tab bar aggregates the running-indicator dot per tab by
filtering /api/jobs results on workspace_id. If the field is missing from the
list response, the dot logic silently degrades to "no jobs" — this test
prevents that regression.
"""
import importlib.util
import json
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class JobsListWorkspaceIdTests(unittest.TestCase):
    def _run(self, module, lock_attr: str) -> list[dict]:
        jobs = getattr(module, "JOBS")
        lock = getattr(module, lock_attr)

        jobs.clear()
        with lock:
            jobs["job-a"] = {
                "status": "running",
                "model": "m1",
                "params": {"prompt": "hello"},
                "prompt": "hello",
                "created_at": "",
                "submitted_at": 1000,
                "started_at": 1001,
                "finished_at": None,
                "username": "alice",
                "workspace_id": "client-a",
                "results": [],
                "errors": [],
                "done": 0,
                "total": 1,
            }
            jobs["job-b"] = {
                "status": "succeeded",
                "model": "m1",
                "params": {"prompt": "world"},
                "prompt": "world",
                "created_at": "",
                "submitted_at": 2000,
                "started_at": 2001,
                "finished_at": 2100,
                "username": "alice",
                "workspace_id": "client-b",
                "results": [],
                "errors": [],
                "done": 1,
                "total": 1,
            }

        # Bind to ephemeral port; ThreadingHTTPServer handles requests via the
        # sub-app's Handler exactly as it does in production.
        server = ThreadingHTTPServer(("127.0.0.1", 0), module.Handler)
        host, port = server.server_address
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            req = urllib.request.Request(
                f"http://{host}:{port}/api/jobs",
                headers={"X-Is-Admin": "1"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                payload = json.loads(resp.read().decode("utf-8"))
        finally:
            server.shutdown()
            server.server_close()
            jobs.clear()

        self.assertTrue(payload.get("ok"))
        return payload.get("jobs") or []

    def _assert_workspace_ids(self, items: list[dict]) -> None:
        by_id = {it["job_id"]: it for it in items}
        self.assertIn("job-a", by_id)
        self.assertIn("job-b", by_id)
        self.assertEqual(by_id["job-a"]["workspace_id"], "client-a")
        self.assertEqual(by_id["job-b"]["workspace_id"], "client-b")

    def test_seedance_jobs_list_includes_workspace_id(self):
        module = load_module("seedance_jobs_under_test", ROOT / "seedance" / "app.py")
        items = self._run(module, "JOBS_LOCK")
        self._assert_workspace_ids(items)

    def test_nano_jobs_list_includes_workspace_id(self):
        module = load_module("nano_jobs_under_test", ROOT / "nano-banana" / "app.py")
        items = self._run(module, "LOCK")
        self._assert_workspace_ids(items)


if __name__ == "__main__":
    unittest.main()
