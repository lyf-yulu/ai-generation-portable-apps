"""Partial-success jobs must still count the items they really generated.

Sub-apps report final_status="failed" as soon as a single item errors, so a job
that produced 3 of 4 images arrives at finalize_job as a failure. The daily.jobs
rollback is correct there, but the per-item images/seconds tally lives in the
poll loop — dropping the job from _pending_jobs would lose those 3 billed items.
"""

import importlib.util
import json
import threading
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PORTAL_APP = ROOT / "portal" / "app.py"


def load_portal_module():
    spec = importlib.util.spec_from_file_location("portal_app_partial_usage", PORTAL_APP)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakeResponse:
    def __init__(self, payload: dict):
        self.status = 200
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._payload


class FakeConnection:
    def __init__(self, payload: dict):
        self._payload = payload

    def request(self, *args, **kwargs):
        return None

    def getresponse(self) -> FakeResponse:
        return FakeResponse(self._payload)

    def close(self):
        return None


class PartialSuccessUsageTests(unittest.TestCase):
    """Drive the real UsageTracker methods without its background threads.

    ``__init__`` starts a flusher plus two loops and binds to the production
    usage.json, so tests build the instance directly and stub ``_save``.
    """

    @classmethod
    def setUpClass(cls):
        cls.module = load_portal_module()

    def _tracker(self):
        tracker = self.module.UsageTracker.__new__(self.module.UsageTracker)
        tracker._lock = threading.Lock()
        tracker._data = self.module.UsageTracker._empty_data()
        tracker._pending_jobs = []
        tracker._dirty = False
        tracker._flush_wake = threading.Event()
        tracker._save = lambda: None
        return tracker

    def _drain_poll_once(self, tracker, payload: dict):
        """Run exactly one iteration of the poll loop body against a fake sub-app."""
        conn = FakeConnection(payload)
        # The loop sleeps first, so the second sleep call ends the iteration.
        sleeps = {"n": 0}

        def fake_sleep(_seconds):
            sleeps["n"] += 1
            if sleeps["n"] > 1:
                raise StopIteration

        fake_apps = {"dreamina": {"port": 18888}}
        with mock.patch.object(self.module.time, "sleep", side_effect=fake_sleep), \
             mock.patch.object(self.module.http.client, "HTTPConnection", return_value=conn), \
             mock.patch.object(self.module, "APPS", fake_apps):
            with self.assertRaises(StopIteration):
                tracker._job_poll_loop()

    def _only_day(self, section: dict) -> dict:
        self.assertEqual(len(section), 1, f"expected exactly one date bucket, got {section!r}")
        return next(iter(section.values()))

    def test_partial_success_stays_pending_after_failed_finalize(self):
        tracker = self._tracker()
        tracker.register_job("dreamina", "job-partial", "alice", job_type="image")
        tracker.inc_daily_jobs("dreamina")

        # Sub-app reports failure because one of four items errored.
        tracker.finalize_job("dreamina", "job-partial", "failed")

        self.assertTrue(
            any(j["job_id"] == "job-partial" for j in tracker._pending_jobs),
            "a failed-but-partial job must stay pending so the poll loop can tally its output",
        )

    def test_partial_success_counts_generated_images(self):
        tracker = self._tracker()
        tracker.register_job("dreamina", "job-partial", "alice", job_type="image")
        tracker.inc_daily_jobs("dreamina")
        tracker.finalize_job("dreamina", "job-partial", "failed")

        self._drain_poll_once(tracker, {"status": "failed", "done": 3, "total": 4})

        day = self._only_day(tracker._data.get("by_user", {}))
        self.assertEqual(day["alice"]["dreamina"]["images"], 3)

    def test_partial_success_counts_video_seconds(self):
        tracker = self._tracker()
        tracker.register_job("dreamina", "job-vid", "dave", job_type="video", duration_per_item=5)
        tracker.inc_daily_jobs("dreamina")
        tracker.finalize_job("dreamina", "job-vid", "failed")

        self._drain_poll_once(tracker, {"status": "failed", "done": 2, "total": 3, "duration": 5})

        day = self._only_day(tracker._data.get("by_user", {}))
        self.assertEqual(day["dave"]["dreamina"]["seconds"], 10)

    def test_pure_failure_counts_nothing(self):
        tracker = self._tracker()
        tracker.register_job("dreamina", "job-dead", "bob", job_type="image")
        tracker.inc_daily_jobs("dreamina")
        tracker.finalize_job("dreamina", "job-dead", "failed")

        self._drain_poll_once(tracker, {"status": "failed", "done": 0, "total": 2})

        self.assertEqual(tracker._data.get("by_user", {}), {})

    def test_polled_job_is_dropped_from_pending(self):
        tracker = self._tracker()
        tracker.register_job("dreamina", "job-partial", "alice", job_type="image")
        tracker.inc_daily_jobs("dreamina")
        tracker.finalize_job("dreamina", "job-partial", "failed")

        self._drain_poll_once(tracker, {"status": "failed", "done": 3, "total": 4})

        self.assertEqual(tracker._pending_jobs, [], "poll loop owns pending cleanup once tallied")
        day = self._only_day(tracker._data.get("by_user", {}))
        self.assertEqual(day["alice"]["dreamina"]["images"], 3, "must not double-count")

    def test_daily_jobs_rollback_stays_idempotent(self):
        tracker = self._tracker()
        tracker.register_job("dreamina", "job-once", "carol", job_type="image")
        tracker.inc_daily_jobs("dreamina")

        self.assertTrue(tracker.finalize_job("dreamina", "job-once", "failed"))
        self.assertFalse(tracker.finalize_job("dreamina", "job-once", "failed"))

        day = self._only_day(tracker._data.get("daily", {}))
        self.assertEqual(day["dreamina"]["jobs"], 0)

    def test_successful_job_counts_normally(self):
        tracker = self._tracker()
        tracker.register_job("dreamina", "job-good", "erin", job_type="image")
        tracker.inc_daily_jobs("dreamina")
        tracker.finalize_job("dreamina", "job-good", "completed")

        self._drain_poll_once(tracker, {"status": "completed", "done": 4, "total": 4})

        day = self._only_day(tracker._data.get("by_user", {}))
        self.assertEqual(day["erin"]["dreamina"]["images"], 4)
        daily = self._only_day(tracker._data.get("daily", {}))
        self.assertEqual(daily["dreamina"]["jobs"], 1, "success must not roll back daily.jobs")


if __name__ == "__main__":
    unittest.main()
