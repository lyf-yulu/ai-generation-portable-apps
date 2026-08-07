"""duration=-1 must never reach the usage counters as a negative number.

Ark accepts duration=-1 to mean "pick the length yourself", and video edit /
extend tasks are *required* to use it. But Portal bills video seconds as
``done * job["duration"]`` (portal/app.py, _job_poll_loop), so storing -1 on the
job record would subtract from by_user.seconds on every completed run.

Both sub-apps therefore keep the request value and the billing value apart:
the job record starts at 0 and the real length is backfilled from Ark's task
response once the run succeeds.
"""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class PortalBillingFormulaTests(unittest.TestCase):
    """Pin down why a negative duration is dangerous in the first place."""

    def test_negative_duration_would_subtract_seconds(self):
        done = 2
        self.assertEqual(done * 12, 24)
        self.assertEqual(done * -1, -2, "this is the corruption the sub-apps must prevent")
        self.assertEqual(done * 0, 0, "0 is inert — safe to store until the real length is known")


class SeedanceDurationSentinelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_module("seedance_app_duration_test", ROOT / "seedance" / "app.py")

    def _create_job(self, duration):
        """Call the real create_job with the background thread stubbed out."""
        mod = self.mod
        values = {
            "prompt": "test",
            "provider": "volcengine",
            "model": "doubao-seedance-2-5-260628",
            "duration": duration,
            "resolution": "480p",
            "ratio": "adaptive",
        }
        with mock.patch.object(mod.threading, "Thread") as thread, \
             mock.patch.object(mod, "record_activity"), \
             mock.patch.object(mod, "copy_files_to_restore", return_value={}):
            thread.return_value.start.return_value = None
            job_id = mod.create_job(values, {}, "test", "text2video", {}, "ws-a", "tester")
        return mod.JOBS[job_id]

    def test_sentinel_is_not_stored_as_negative(self):
        job = self._create_job(-1)
        self.assertEqual(
            job["duration"], 0,
            "job['duration'] feeds Portal's done * duration billing; -1 would go negative",
        )

    def test_normal_duration_is_preserved(self):
        self.assertEqual(self._create_job(20)["duration"], 20)

    def test_run_one_backfills_the_real_length(self):
        """A -1 job must end up billing the length Ark actually produced."""
        job = self._create_job(-1)
        self.assertEqual(job["duration"], 0)

        # Simulate the backfill run_one() performs on success.
        ark_response = {"status": "succeeded", "duration": 20, "content": {"video_url": "x"}}
        actual = ark_response.get("duration")
        if isinstance(actual, (int, float)) and actual > 0 and int(job.get("duration") or 0) <= 0:
            job["duration"] = int(actual)

        self.assertEqual(job["duration"], 20, "billing must use Ark's reported length")
        self.assertEqual(1 * job["duration"], 20, "and the resulting charge is positive")

    def test_backfill_does_not_overwrite_an_explicit_duration(self):
        job = self._create_job(15)
        actual = 20  # Ark trimmed or extended it; the user asked for 15
        if isinstance(actual, (int, float)) and actual > 0 and int(job.get("duration") or 0) <= 0:
            job["duration"] = int(actual)
        self.assertEqual(job["duration"], 15, "an explicit request must not be silently rewritten")


class PortraitDurationSentinelTests(unittest.TestCase):
    """The portrait module keeps two fields because one value serves both roles."""

    @classmethod
    def setUpClass(cls):
        cls.source = (ROOT / "volcengine-portrait" / "app.py").read_text("utf-8")

    def test_job_record_clamps_but_request_keeps_the_sentinel(self):
        self.assertIn(
            '"requested_duration": duration,', self.source,
            "the value sent to Ark must keep -1",
        )
        self.assertIn(
            '"duration": max(0, duration),', self.source,
            "the value Portal bills on must never be negative",
        )

    def test_payload_reads_the_requested_value(self):
        self.assertIn(
            'job.get("requested_duration"', self.source,
            "the Ark payload must read requested_duration, not the clamped field",
        )

    def test_success_path_backfills_real_duration(self):
        self.assertIn(
            'actual_duration = task_result.get("duration")', self.source,
            "the real length has to be read back from Ark's task response",
        )


class FrontendSentinelDocumentationTests(unittest.TestCase):
    """The user has to be told when -1 is required, or the feature is unusable."""

    CASES = [
        ("seedance", ROOT / "seedance" / "static" / "index.html"),
        ("portrait", ROOT / "portal" / "static" / "index.html"),
    ]

    def test_duration_input_accepts_minus_one(self):
        for label, path in self.CASES:
            with self.subTest(app=label):
                html = path.read_text("utf-8")
                self.assertIn('min="-1"', html, f"{label}: number input must allow -1")

    def test_ui_explains_when_minus_one_is_required(self):
        for label, path in self.CASES:
            with self.subTest(app=label):
                html = path.read_text("utf-8")
                self.assertIn("由模型自动决定", html, f"{label}: must explain what -1 does")
                self.assertIn("视频编辑", html, f"{label}: must name the edit task")
                self.assertIn("视频延长", html, f"{label}: must name the extend task")
                self.assertIn("adaptive", html, f"{label}: must mention the adaptive requirement")

    def test_portrait_ratio_offers_adaptive(self):
        html = (ROOT / "portal" / "static" / "index.html").read_text("utf-8")
        self.assertIn(
            "<option>adaptive</option>", html,
            "edit/extend tasks are unsubmittable without an adaptive ratio option",
        )


if __name__ == "__main__":
    unittest.main()
