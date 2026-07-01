import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
PORTAL_APP = ROOT / "portal" / "app.py"

def load_portal_module(state_dir: Path):
    spec = importlib.util.spec_from_file_location("portal_app_under_test", PORTAL_APP)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    module.STATE_DIR = state_dir
    module.USAGE_PATH = state_dir / "usage.json"
    return module


class UsageJsonlTests(unittest.TestCase):
    def test_record_appends_daily_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            module = load_portal_module(base)
            tracker = module.UsageTracker()
            with mock.patch.object(module.time, "strftime", side_effect=lambda fmt: {
                "%Y-%m-%d": "2026-06-30",
                "%Y-%m-%d %H:%M:%S": "2026-06-30 09:00:00",
            }[fmt]):
                tracker.record("seedance", "10.0.0.1", "POST", "/api/jobs", "alice")

            jsonl = base / "logs" / "usage-2026-06-30.jsonl"
            self.assertTrue(jsonl.exists())
            lines = jsonl.read_text("utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            row = json.loads(lines[0])
            self.assertEqual(row["app"], "seedance")
            self.assertEqual(row["username"], "alice")
            self.assertEqual(row["path"], "/api/jobs")

    def test_record_jsonl_write_failure_does_not_break_usage_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            module = load_portal_module(base)
            tracker = module.UsageTracker()
            with mock.patch.object(module, "_append_usage_jsonl", side_effect=OSError("disk full")):
                tracker.record("seedance", "10.0.0.1", "POST", "/api/jobs", "alice")
            self.assertTrue((base / "usage.json").exists())
            data = json.loads((base / "usage.json").read_text("utf-8"))
            self.assertEqual(len(data["records"]), 1)


if __name__ == "__main__":
    unittest.main()
