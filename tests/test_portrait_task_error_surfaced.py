"""Portrait job errors must surface Ark's real reason, not just "failed".

Before this fix the failed branch only recorded ``f"Run {idx}: {t_status}"``,
so a content-policy rejection or a resource-limit refusal looked identical to
a generic failure. The user had to hit Ark directly to find out what went
wrong (that's how "生成图中人物在房间中和外星怪物战斗的视频" turned into a
mystery "Run 0: failed" in production).
"""

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load_portrait():
    spec = importlib.util.spec_from_file_location(
        "portrait_error_surface_test", ROOT / "volcengine-portrait" / "app.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["portrait_error_surface_test"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class PortraitFailedTaskErrorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_portrait()

    def _run(self, task_result: dict) -> dict:
        """Drive _run_virtual_job_impl through one failed poll iteration."""
        mod = self.mod
        job = {
            "job_id": "job-x", "activity_id": "act-x", "task_type": "virtual",
            "asset_id": "asset-1", "extra_asset_ids": [],
            "prompt": "test", "model": "doubao-seedance-2-5-260628",
            "requested_duration": 5, "duration": 5,
            "resolution": "480p", "ratio": "adaptive",
            "status": "queued", "total": 1, "done": 0,
            "results": [], "errors": [],
            "extra_image_urls": [], "events": [],
            "created_at": "", "submitted_at": 0.0,
            "started_at": None, "finished_at": None,
            "username": "tester", "api_key": None,
            "output_dir": "/tmp",
        }

        # The impl calls _asset_content_item -> _asset_type_for, which resolves
        # the asset's real modality. Short-circuit that to a plain image ref.
        with mock.patch.object(mod, "_asset_content_item",
                               return_value={"type": "image_url", "image_url": {"url": "asset://x"}, "role": "reference_image"}), \
             mock.patch.object(mod, "ark_v3_call",
                               side_effect=[{"id": "cgt-test"}, task_result]), \
             mock.patch.object(mod, "update_activity"), \
             mock.patch.object(mod, "report_final_to_portal"), \
             mock.patch.object(mod.time, "sleep"):
            mod._run_virtual_job_impl("job-x", job)
        return job

    def test_error_message_is_recorded(self):
        job = self._run({
            "status": "failed",
            "error": {
                "code": "OutputVideoSensitiveContentDetected.PolicyViolation",
                "message": "The request failed because the output video may be related to copyright restrictions.",
            },
        })
        joined = " | ".join(job["errors"])
        self.assertIn("copyright restrictions", joined,
                      "Ark's message must reach the user — this is the whole point of the fix")

    def test_falls_back_to_error_code_when_message_missing(self):
        job = self._run({"status": "failed", "error": {"code": "SomethingBroke"}})
        joined = " | ".join(job["errors"])
        self.assertIn("SomethingBroke", joined)

    def test_generic_failure_stays_readable(self):
        """A task with no error object must not corrupt the summary."""
        job = self._run({"status": "failed"})
        self.assertTrue(job["errors"], "at least one error entry must still be recorded")
        self.assertIn("failed", job["errors"][0])

    def test_error_also_lands_in_events_for_the_UI(self):
        job = self._run({
            "status": "failed",
            "error": {"message": "quota exhausted for today"},
        })
        event_msgs = " | ".join(e.get("message", "") for e in job["events"])
        self.assertIn("quota exhausted", event_msgs,
                      "the UI reads events; a bare errors[] entry stays hidden until the user opens details")


if __name__ == "__main__":
    unittest.main()
