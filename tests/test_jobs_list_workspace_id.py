"""Verify /api/jobs list responses expose the fields the frontend needs.

Two contracts guarded here:

1. workspace_id — the in-app tab bar filters running-indicator dots per tab
   on this field. Missing → dots silently degrade to "no jobs".

2. results[] with download URLs — loadJobs() rebuilds image/video cards from
   the list rows (5s refresh + tab-switch restore). Seedance uses a flat
   results[].download_url shape; nano-banana nests inside results[].images[].
   Either dropping the field would silently render "succeeded" jobs with no
   media.
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
                "results": [
                    {
                        "index": 0,
                        "task_id": "t1",
                        "status": "succeeded",
                        "download_url": "/api/download/aaa",  # seedance flat shape
                        "filename": "out.mp4",
                        "images": [  # nano-banana nested shape
                            {"download_url": "/api/download/bbb", "filename": "out.png"},
                        ],
                    },
                ],
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
        # The list endpoint MUST include results[].images[].download_url so
        # that loadJobs() (5s refresh + tab-switch restore) can rebuild the
        # image cards. Dropping it would silently show "succeeded" jobs with
        # no images after any refresh.
        job_b = next(it for it in items if it["job_id"] == "job-b")
        self.assertEqual(len(job_b["results"]), 1)
        images = job_b["results"][0]["images"]
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0]["download_url"], "/api/download/bbb")
        self.assertEqual(images[0]["filename"], "out.png")

    def test_seedance_jobs_list_includes_download_url(self):
        module = load_module("seedance_jobs_dl_url", ROOT / "seedance" / "app.py")
        items = self._run(module, "JOBS_LOCK")
        # seedance uses a flat results[] shape with download_url on the row.
        # loadJobs() reads r.download_url directly, so the list endpoint
        # must expose it.
        job_b = next(it for it in items if it["job_id"] == "job-b")
        self.assertEqual(len(job_b["results"]), 1)
        self.assertEqual(job_b["results"][0]["download_url"], "/api/download/aaa")
        self.assertEqual(job_b["results"][0]["filename"], "out.mp4")


if __name__ == "__main__":
    unittest.main()
