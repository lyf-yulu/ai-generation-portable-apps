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


class UsageJsonlPruneTests(unittest.TestCase):
    def test_prune_deletes_files_older_than_retention(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            module = load_portal_module(base)
            logs = base / "logs"
            logs.mkdir()
            old = logs / "usage-2026-05-01.jsonl"
            recent = logs / "usage-2026-06-29.jsonl"
            old.write_text("{}\n", "utf-8")
            recent.write_text("{}\n", "utf-8")
            module._prune_old_usage_jsonl("2026-06-30")
            self.assertFalse(old.exists(), "file older than 30 days should be pruned")
            self.assertTrue(recent.exists(), "file within retention should stay")

    def test_prune_ignores_files_with_bad_date_stem(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            module = load_portal_module(base)
            logs = base / "logs"
            logs.mkdir()
            bad = logs / "usage-notadate.jsonl"
            bad.write_text("{}\n", "utf-8")
            # Must not raise
            module._prune_old_usage_jsonl("2026-06-30")
            self.assertTrue(bad.exists(), "malformed filename should be skipped, not deleted")


def load_daily_report_module():
    spec = importlib.util.spec_from_file_location(
        "daily_report_under_test",
        ROOT / "portal" / "daily_report.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class AggregationTests(unittest.TestCase):
    SAMPLE = [
        {"time": "2026-06-30 09:00:00", "app": "seedance",    "ip": "10.0.0.1", "username": "alice", "method": "POST", "path": "/api/jobs"},
        {"time": "2026-06-30 09:00:05", "app": "seedance",    "ip": "10.0.0.1", "username": "alice", "method": "GET",  "path": "/api/jobs/abc"},
        {"time": "2026-06-30 14:20:00", "app": "nano-banana", "ip": "10.0.0.2", "username": "bob",   "method": "POST", "path": "/api/jobs"},
        {"time": "2026-06-30 14:22:00", "app": "nano-banana", "ip": "10.0.0.2", "username": "bob",   "method": "GET",  "path": "/api/download/xyz"},
    ]

    def _write_sample_jsonl(self, base: Path, date: str, rows: list) -> Path:
        logs = base / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        p = logs / f"usage-{date}.jsonl"
        with p.open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return p

    def test_load_events_from_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self._write_sample_jsonl(base, "2026-06-30", self.SAMPLE)
            mod = load_daily_report_module()
            events, source = mod.load_events(base, "2026-06-30")
            self.assertEqual(len(events), 4)
            self.assertEqual(source, "jsonl")

    def test_load_events_fallback_to_usage_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "usage.json").write_text(json.dumps({"records": self.SAMPLE}), "utf-8")
            mod = load_daily_report_module()
            events, source = mod.load_events(base, "2026-06-30")
            self.assertEqual(len(events), 4)
            self.assertEqual(source, "fallback")

    def test_aggregate(self):
        mod = load_daily_report_module()
        agg = mod.aggregate(self.SAMPLE, "2026-06-30")
        self.assertEqual(agg["date"], "2026-06-30")
        self.assertEqual(agg["total_events"], 4)
        self.assertEqual(agg["unique_users"], 2)
        self.assertEqual(agg["by_app"]["seedance"]["requests"], 2)
        self.assertEqual(agg["by_app"]["nano-banana"]["submits"], 1)
        self.assertEqual(agg["by_app"]["nano-banana"]["downloads"], 1)
        self.assertEqual(len(agg["hourly"]), 24)
        self.assertEqual(agg["hourly"][9], 2)
        self.assertEqual(agg["hourly"][14], 2)
        self.assertIn(agg["peak_hour"], (9, 14))
        top = {u["username"] for u in agg["by_user"]}
        self.assertEqual(top, {"alice", "bob"})

    def test_write_csv_bom_and_derived_event_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            mod = load_daily_report_module()
            csv_path = mod.write_csv(base, "2026-06-30", self.SAMPLE)
            self.assertTrue(csv_path.exists())
            raw = csv_path.read_bytes()
            self.assertTrue(raw.startswith(b"\xef\xbb\xbf"), "csv must start with UTF-8 BOM")
            text = raw.decode("utf-8-sig")
            lines = text.strip().splitlines()
            self.assertEqual(lines[0], "timestamp,app,username,ip,method,path,event_type")
            self.assertEqual(len(lines), 5)
            self.assertIn("submit_job", lines[1])
            self.assertIn("poll", lines[2])
            self.assertIn("submit_job", lines[3])
            self.assertIn("download", lines[4])


if __name__ == "__main__":
    unittest.main()
