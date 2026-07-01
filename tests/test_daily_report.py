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
    # Business-metric shape: {username: {app: {images, seconds}}}
    SAMPLE = {
        "alice": {
            "seedance": {"images": 0, "seconds": 60},
            "nano-banana": {"images": 12, "seconds": 0},
        },
        "bob": {
            "nano-banana": {"images": 5, "seconds": 0},
            "volcengine-portrait": {"images": 0, "seconds": 30},
        },
    }

    def _write_usage_json(self, base: Path, date: str, by_user: dict):
        (base / "usage.json").write_text(
            json.dumps({"by_user": {date: by_user}}, ensure_ascii=False), "utf-8")

    def test_load_usage_data_reads_by_user_slice(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self._write_usage_json(base, "2026-06-30", self.SAMPLE)
            mod = load_daily_report_module()
            by_user, source = mod.load_usage_data(base, "2026-06-30")
            self.assertEqual(source, "usage_json")
            self.assertEqual(set(by_user.keys()), {"alice", "bob"})

    def test_load_usage_data_missing_date_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self._write_usage_json(base, "2026-06-29", self.SAMPLE)
            mod = load_daily_report_module()
            by_user, source = mod.load_usage_data(base, "2026-06-30")
            self.assertEqual(by_user, {})
            self.assertEqual(source, "missing")

    def test_load_usage_data_no_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            mod = load_daily_report_module()
            by_user, source = mod.load_usage_data(base, "2026-06-30")
            self.assertEqual(by_user, {})
            self.assertEqual(source, "missing")

    def test_aggregate(self):
        mod = load_daily_report_module()
        agg = mod.aggregate(self.SAMPLE, "2026-06-30")
        self.assertEqual(agg["date"], "2026-06-30")
        self.assertEqual(agg["total_images"], 17)     # 12 + 5
        self.assertEqual(agg["total_seconds"], 90)    # 60 + 30
        self.assertEqual(agg["unique_users"], 2)
        self.assertEqual(agg["by_app"]["nano-banana"]["images"], 17)
        self.assertEqual(agg["by_app"]["nano-banana"]["users"], 2)
        self.assertEqual(agg["by_app"]["seedance"]["seconds"], 60)
        self.assertEqual(agg["by_app"]["volcengine-portrait"]["seconds"], 30)
        top = {u["username"] for u in agg["by_user"]}
        self.assertEqual(top, {"alice", "bob"})
        # alice has more seconds (60) than bob (30), so she ranks first.
        self.assertEqual(agg["by_user"][0]["username"], "alice")

    def test_aggregate_skips_zero_stat_rows(self):
        mod = load_daily_report_module()
        sample = {
            "carol": {
                "seedance": {"images": 0, "seconds": 0},   # empty row, should be skipped
                "nano-banana": {"images": 3, "seconds": 0},
            }
        }
        agg = mod.aggregate(sample, "2026-06-30")
        self.assertNotIn("seedance", agg["by_app"])
        self.assertEqual(agg["by_user"][0]["apps"], ["nano-banana"])

    def test_write_csv_bom_and_business_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            mod = load_daily_report_module()
            csv_path = mod.write_csv(base, "2026-06-30", self.SAMPLE)
            self.assertTrue(csv_path.exists())
            raw = csv_path.read_bytes()
            self.assertTrue(raw.startswith(b"\xef\xbb\xbf"), "csv must start with UTF-8 BOM")
            text = raw.decode("utf-8-sig")
            lines = text.strip().splitlines()
            self.assertEqual(lines[0], "date,username,app,images,seconds")
            # 2 users × 2 non-zero apps each = 4 rows
            self.assertEqual(len(lines), 5)
            self.assertIn("2026-06-30,alice,nano-banana,12,0", text)
            self.assertIn("2026-06-30,alice,seedance,0,60", text)
            self.assertIn("2026-06-30,bob,volcengine-portrait,0,30", text)


class InsightTests(unittest.TestCase):
    AGG_STUB = {
        "date": "2026-06-30", "total_images": 12, "total_seconds": 60, "unique_users": 2,
        "by_app": {"nano-banana": {"images": 12, "seconds": 0, "users": 2}},
        "by_user": [],
    }
    AGG_EMPTY = {"date": "2026-06-30", "total_images": 0, "total_seconds": 0,
                 "unique_users": 0, "by_app": {}, "by_user": []}

    def test_generate_insight_returns_fallback_when_no_key(self):
        mod = load_daily_report_module()
        result = mod.generate_insight(self.AGG_STUB, deepseek_key="")
        self.assertIn("trend", result)
        self.assertIn("highlight", result)
        self.assertIn("suggestion", result)
        self.assertTrue(result["_fallback"])

    def test_generate_insight_parses_llm_json(self):
        mod = load_daily_report_module()
        fake_response = {
            "choices": [{"message": {"content": json.dumps({
                "trend": "整体平稳",
                "highlight": "seedance 使用集中",
                "suggestion": "关注高峰时段容量",
            })}}]
        }
        with mock.patch.object(mod, "_deepseek_chat", return_value=fake_response):
            result = mod.generate_insight(self.AGG_STUB, deepseek_key="sk-fake")
        self.assertEqual(result["trend"], "整体平稳")
        self.assertFalse(result.get("_fallback"))

    def test_generate_insight_handles_llm_failure(self):
        mod = load_daily_report_module()
        with mock.patch.object(mod, "_deepseek_chat", side_effect=RuntimeError("boom")):
            result = mod.generate_insight(self.AGG_EMPTY, deepseek_key="sk-fake")
        self.assertTrue(result["_fallback"])
        self.assertIn("trend", result)

    def test_generate_insight_handles_bad_json(self):
        mod = load_daily_report_module()
        fake_response = {"choices": [{"message": {"content": "not json at all"}}]}
        with mock.patch.object(mod, "_deepseek_chat", return_value=fake_response):
            result = mod.generate_insight(self.AGG_EMPTY, deepseek_key="sk-fake")
        self.assertTrue(result["_fallback"])


class CardBuildTests(unittest.TestCase):
    AGG = {
        "date": "2026-06-30",
        "total_images": 1234,
        "total_seconds": 567,
        "unique_users": 7,
        "by_app": {
            "nano-banana":         {"images": 800, "seconds": 0,   "users": 4},
            "seedance":            {"images": 0,   "seconds": 300, "users": 3},
            "volcengine-portrait": {"images": 434, "seconds": 267, "users": 2},
        },
        "by_user": [
            {"username": "高大王", "images": 500, "seconds": 120, "apps": ["nano-banana", "volcengine-portrait"]},
        ],
    }
    INSIGHT = {"trend": "T", "highlight": "H", "suggestion": "S"}

    def test_build_card_structure(self):
        mod = load_daily_report_module()
        card = mod.build_card(self.AGG, self.INSIGHT, csv_url="https://portal.example/api/reports/daily/2026-06-30.csv")
        self.assertEqual(card["msg_type"], "interactive")
        payload = card["card"]
        self.assertEqual(payload["schema"], "2.0")
        blob = json.dumps(payload, ensure_ascii=False)
        self.assertIn("2026-06-30", blob)
        self.assertIn("1,234", blob)             # total_images formatted
        self.assertIn("567", blob)               # total_seconds
        self.assertIn("nano-banana", blob)
        self.assertIn("seedance", blob)
        self.assertIn("高大王", blob)
        self.assertIn("https://portal.example/api/reports/daily/2026-06-30.csv", blob)
        self.assertIn("T", blob)
        self.assertIn("H", blob)
        self.assertIn("S", blob)

    def test_sign_webhook_body_matches_feishu_algo(self):
        mod = load_daily_report_module()
        sig = mod.sign_webhook_body("secret123", 1700000000)
        expected_string = "1700000000\nsecret123"
        import hmac, hashlib, base64
        expected = base64.b64encode(hmac.new(expected_string.encode(), digestmod=hashlib.sha256).digest()).decode()
        self.assertEqual(sig, expected)


class WebhookSendTests(unittest.TestCase):
    def test_send_webhook_signs_when_secret_present(self):
        mod = load_daily_report_module()
        captured = {}
        class FakeResp:
            def __enter__(self_inner): return self_inner
            def __exit__(self_inner, *a): return False
            def read(self_inner): return b'{"code":0,"msg":"ok"}'
        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode())
            return FakeResp()
        with mock.patch.object(mod.urllib.request, "urlopen", side_effect=fake_urlopen):
            ok, info = mod.send_webhook("https://open.feishu.cn/x", {"msg_type": "interactive", "card": {}}, sign_secret="s")
        self.assertTrue(ok)
        self.assertIn("timestamp", captured["body"])
        self.assertIn("sign", captured["body"])

    def test_send_webhook_no_sign_when_secret_empty(self):
        mod = load_daily_report_module()
        captured = {}
        class FakeResp:
            def __enter__(self_inner): return self_inner
            def __exit__(self_inner, *a): return False
            def read(self_inner): return b'{"code":0}'
        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode())
            return FakeResp()
        with mock.patch.object(mod.urllib.request, "urlopen", side_effect=fake_urlopen):
            ok, _ = mod.send_webhook("https://x", {"msg_type": "interactive", "card": {}}, sign_secret="")
        self.assertTrue(ok)
        self.assertNotIn("sign", captured["body"])

    def test_send_webhook_reports_feishu_error(self):
        mod = load_daily_report_module()
        class FakeResp:
            def __enter__(self_inner): return self_inner
            def __exit__(self_inner, *a): return False
            def read(self_inner): return b'{"code":19024,"msg":"sign error"}'
        with mock.patch.object(mod.urllib.request, "urlopen", return_value=FakeResp()):
            ok, info = mod.send_webhook("https://x", {"msg_type": "interactive", "card": {}}, sign_secret="")
        self.assertFalse(ok)
        self.assertIn("19024", info)


class SendDailyReportTests(unittest.TestCase):
    def _seed(self, base: Path):
        payload = {
            "by_user": {
                "2026-06-30": {
                    "alice": {"seedance": {"images": 0, "seconds": 45}},
                    "bob": {"nano-banana": {"images": 8, "seconds": 0}},
                }
            }
        }
        (base / "usage.json").write_text(json.dumps(payload, ensure_ascii=False), "utf-8")

    def test_send_daily_report_dry_run_writes_csv_no_webhook(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self._seed(base)
            mod = load_daily_report_module()
            calls = []
            with mock.patch.object(mod, "send_webhook", side_effect=lambda *a, **kw: calls.append(a) or (True, "ok")):
                result = mod.send_daily_report(base, "2026-06-30",
                                               config={"webhook_url": "https://x", "portal_base_url": "https://p"},
                                               deepseek_key="",
                                               dry_run=True)
            self.assertTrue(result["ok"])
            self.assertTrue((base / "reports" / "2026-06-30.csv").exists())
            self.assertEqual(calls, [], "dry_run must not call send_webhook")

    def test_send_daily_report_real_sends_when_not_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self._seed(base)
            mod = load_daily_report_module()
            with mock.patch.object(mod, "send_webhook", return_value=(True, "ok")) as sender:
                result = mod.send_daily_report(base, "2026-06-30",
                                               config={"webhook_url": "https://x", "portal_base_url": "https://p:9091"},
                                               deepseek_key="",
                                               dry_run=False)
            self.assertTrue(result["ok"])
            sender.assert_called_once()
            args, _ = sender.call_args
            self.assertEqual(args[0], "https://x")
            self.assertIn("2026-06-30", json.dumps(args[1], ensure_ascii=False))

    def test_csv_url_uses_portal_base_when_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self._seed(base)
            mod = load_daily_report_module()
            result = mod.send_daily_report(base, "2026-06-30",
                                           config={"webhook_url": "", "portal_base_url": "https://portal.internal:9090/"},
                                           deepseek_key="",
                                           dry_run=True)
            blob = json.dumps(result["card"], ensure_ascii=False)
            self.assertIn("https://portal.internal:9090/api/reports/daily/2026-06-30.csv", blob)

    def test_csv_url_falls_back_to_lan_url_when_config_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self._seed(base)
            mod = load_daily_report_module()
            with mock.patch.object(mod, "_default_portal_base_url", return_value="https://192.168.1.2:9090"):
                result = mod.send_daily_report(base, "2026-06-30",
                                               config={"webhook_url": "", "portal_base_url": ""},
                                               deepseek_key="",
                                               dry_run=True)
            blob = json.dumps(result["card"], ensure_ascii=False)
            self.assertIn("https://192.168.1.2:9090/api/reports/daily/2026-06-30.csv", blob)
            # Guard against regressions where the button URL becomes a bare path
            self.assertNotIn('"url": "/api/reports', blob)


class ConfigTests(unittest.TestCase):
    def test_load_config_returns_defaults_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            mod = load_daily_report_module()
            cfg = mod.load_config(base)
            self.assertFalse(cfg["enabled"])
            self.assertEqual(cfg["schedule_time"], "09:05")
            self.assertEqual(cfg["webhook_url"], "")

    def test_save_config_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            mod = load_daily_report_module()
            mod.save_config(base, {"enabled": True, "webhook_url": "https://x",
                                    "sign_secret": "s", "schedule_time": "10:00",
                                    "portal_base_url": "https://p"})
            cfg = mod.load_config(base)
            self.assertTrue(cfg["enabled"])
            self.assertEqual(cfg["webhook_url"], "https://x")
            self.assertEqual(cfg["schedule_time"], "10:00")


class SchedulerMarkerTests(unittest.TestCase):
    def test_should_run_true_when_time_match_and_no_marker(self):
        mod = load_daily_report_module()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self.assertTrue(mod._should_run_now(base, "09:05", now_hhmm="09:05", today="2026-07-01"))

    def test_should_run_false_when_time_mismatch(self):
        mod = load_daily_report_module()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self.assertFalse(mod._should_run_now(base, "09:05", now_hhmm="09:06", today="2026-07-01"))

    def test_should_run_false_when_marker_exists(self):
        mod = load_daily_report_module()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "reports").mkdir()
            (base / "reports" / ".sent-2026-07-01").write_text("", "utf-8")
            self.assertFalse(mod._should_run_now(base, "09:05", now_hhmm="09:05", today="2026-07-01"))

    def test_mark_sent_creates_marker(self):
        mod = load_daily_report_module()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            mod._mark_sent(base, "2026-07-01")
            self.assertTrue((base / "reports" / ".sent-2026-07-01").exists())


if __name__ == "__main__":
    unittest.main()
