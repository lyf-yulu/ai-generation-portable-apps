import importlib.util
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
DREAMINA_APP = ROOT / "dreamina" / "app.py"


def load_dreamina_module():
    spec = importlib.util.spec_from_file_location("dreamina_app_under_test", DREAMINA_APP)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class DreaminaAccountTests(unittest.TestCase):
    def write_accounts(self, module, base: Path, data: dict):
        module.STATE_DIR = base / "state"
        module.ACCOUNTS_DIR = base / "accounts"
        module.ACCOUNTS_PATH = module.STATE_DIR / "accounts.json"
        module.STATE_DIR.mkdir(parents=True)
        module.ACCOUNTS_DIR.mkdir(parents=True)
        module.ACCOUNTS_PATH.write_text(json.dumps(data), encoding="utf-8")

    def test_sync_system_home_account_takes_over_when_active_account_is_offline(self):
        module = load_dreamina_module()
        with tempfile.TemporaryDirectory() as tmp:
            self.write_accounts(module, Path(tmp), {
                "accounts": [{
                    "id": "acc_old",
                    "name": "账号1",
                    "is_system_home": False,
                    "logged_in": False,
                    "credit": None,
                }],
                "active_account": "acc_old",
                "dispatch_mode": "manual",
            })

            data = module.sync_system_home_account({
                "logged_in": True,
                "credit": {"user_id": 123, "total_credit": 9},
            })

        system_accounts = [a for a in data["accounts"] if a.get("is_system_home")]
        self.assertEqual(len(system_accounts), 1)
        self.assertEqual(data["active_account"], system_accounts[0]["id"])
        self.assertEqual(system_accounts[0]["uid"], 123)
        self.assertEqual(system_accounts[0]["home_dir"], str(Path.home()))

    def test_sync_system_home_account_preserves_fresh_active_account(self):
        module = load_dreamina_module()
        with tempfile.TemporaryDirectory() as tmp:
            self.write_accounts(module, Path(tmp), {
                "accounts": [{
                    "id": "acc_active",
                    "name": "账号1",
                    "is_system_home": False,
                    "logged_in": True,
                    "_login_verified_at": time.time(),
                    "credit": {"user_id": 456},
                }],
                "active_account": "acc_active",
                "dispatch_mode": "manual",
            })

            data = module.sync_system_home_account({
                "logged_in": True,
                "credit": {"user_id": 123, "total_credit": 9},
            })

        self.assertEqual(data["active_account"], "acc_active")
        self.assertTrue(any(a.get("is_system_home") for a in data["accounts"]))

    def test_preflight_missing_home_reports_error(self):
        module = load_dreamina_module()
        with tempfile.TemporaryDirectory() as tmp:
            module.ACCOUNTS_DIR = Path(tmp) / "accounts"
            module.ACCOUNTS_DIR.mkdir()
            result = module.preflight_account_runtime("acc_missing")

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "missing_home")

    def test_preflight_missing_keychain_reports_error_on_macos(self):
        module = load_dreamina_module()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(module.sys, "platform", "darwin"):
            module.ACCOUNTS_DIR = Path(tmp) / "accounts"
            home = module.ACCOUNTS_DIR / "acc_one"
            home.mkdir(parents=True)
            result = module.preflight_account_runtime("acc_one")

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "missing_keychain")

    def test_preflight_deduplicates_project_keychain_entries(self):
        module = load_dreamina_module()
        commands = []
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(module.sys, "platform", "darwin"):
            module.ACCOUNTS_DIR = Path(tmp) / "accounts"
            keychain = module.ACCOUNTS_DIR / "acc_one" / "Library" / "Keychains" / "login.keychain-db"
            keychain.parent.mkdir(parents=True)
            keychain.write_bytes(b"keychain")

            def fake_run(args, **kwargs):
                commands.append(args)

                class Result:
                    returncode = 0
                    stdout = f'"{keychain}"\n"{keychain}"\n"/Users/me/Library/Keychains/login.keychain-db"\n'
                    stderr = ""

                return Result()

            with mock.patch.object(module.subprocess, "run", side_effect=fake_run):
                result = module.preflight_account_runtime("acc_one")

        self.assertTrue(result["ok"])
        set_cmds = [
            cmd for cmd in commands
            if cmd[:4] == ["security", "list-keychains", "-d", "user"] and "-s" in cmd
        ]
        self.assertEqual(set_cmds[-1].count(str(keychain)), 1)

    def test_account_health_records_timeout(self):
        module = load_dreamina_module()
        with tempfile.TemporaryDirectory() as tmp:
            self.write_accounts(module, Path(tmp), {
                "accounts": [{"id": "acc_one", "name": "账号1", "is_system_home": False, "logged_in": False}],
                "active_account": "acc_one",
                "dispatch_mode": "manual",
            })
            with mock.patch.object(module, "preflight_account_runtime", return_value={"ok": True}), \
                 mock.patch.object(module, "get_account_env", return_value={"HOME": "/tmp/account"}), \
                 mock.patch.object(module, "run_cmd", return_value={"returncode": -1, "stdout": "", "stderr": "timeout"}):
                result = module.check_account_health("acc_one")
            saved = module.load_accounts()["accounts"][0]

        self.assertFalse(result["logged_in"])
        self.assertEqual(result["error_code"], "timeout")
        self.assertEqual(saved["last_error_code"], "timeout")

    def test_account_health_marks_valid_account_online(self):
        module = load_dreamina_module()
        with tempfile.TemporaryDirectory() as tmp:
            self.write_accounts(module, Path(tmp), {
                "accounts": [{"id": "acc_one", "name": "账号1", "is_system_home": False, "logged_in": False}],
                "active_account": "acc_one",
                "dispatch_mode": "manual",
            })
            stdout = '{"total_credit": 99, "user_id": 123, "vip_level": "maestro"}'
            with mock.patch.object(module, "preflight_account_runtime", return_value={"ok": True}), \
                 mock.patch.object(module, "get_account_env", return_value={"HOME": "/tmp/account"}), \
                 mock.patch.object(module, "run_cmd", return_value={"returncode": 0, "stdout": stdout, "stderr": ""}):
                result = module.check_account_health("acc_one")
            saved = module.load_accounts()["accounts"][0]

        self.assertTrue(result["logged_in"])
        self.assertEqual(saved["uid"], 123)
        self.assertIsNone(saved["last_error_code"])

    def test_account_health_records_empty_cli_error_returncode(self):
        module = load_dreamina_module()
        with tempfile.TemporaryDirectory() as tmp:
            self.write_accounts(module, Path(tmp), {
                "accounts": [{"id": "acc_one", "name": "账号1", "is_system_home": False, "logged_in": False}],
                "active_account": "acc_one",
                "dispatch_mode": "manual",
            })
            with mock.patch.object(module, "preflight_account_runtime", return_value={"ok": True}), \
                 mock.patch.object(module, "get_account_env", return_value={"HOME": "/tmp/account"}), \
                 mock.patch.object(module, "run_cmd", return_value={"returncode": 1, "stdout": "", "stderr": ""}):
                result = module.check_account_health("acc_one")
            saved = module.load_accounts()["accounts"][0]

        self.assertFalse(result["logged_in"])
        self.assertEqual(result["error_code"], "cli_error")
        self.assertEqual(result["error_detail"], "dreamina user_credit exited with code 1 and no output")
        self.assertEqual(saved["last_error_detail"], "dreamina user_credit exited with code 1 and no output")

    def test_pick_account_for_job_ignores_quarantined_accounts(self):
        module = load_dreamina_module()
        now = time.time()
        with tempfile.TemporaryDirectory() as tmp:
            self.write_accounts(module, Path(tmp), {
                "accounts": [
                    {
                        "id": "acc_bad",
                        "name": "账号1",
                        "is_system_home": False,
                        "logged_in": True,
                        "_login_verified_at": now,
                        "quarantined": True,
                        "last_error_code": "timeout",
                    },
                    {
                        "id": "acc_default",
                        "name": "共享系统账号",
                        "is_system_home": True,
                        "logged_in": True,
                        "_login_verified_at": now,
                    },
                ],
                "active_account": "acc_bad",
                "dispatch_mode": "manual",
            })

            account = module.pick_account_for_job()

        self.assertEqual(account["id"], "acc_default")

    def test_repair_saved_accounts_skips_system_account(self):
        module = load_dreamina_module()
        checked = []
        with tempfile.TemporaryDirectory() as tmp:
            self.write_accounts(module, Path(tmp), {
                "accounts": [
                    {"id": "acc_one", "name": "账号1", "is_system_home": False},
                    {"id": "acc_default", "name": "共享系统账号", "is_system_home": True},
                ],
                "active_account": "acc_default",
                "dispatch_mode": "manual",
            })

            def fake_health(account_id):
                checked.append(account_id)
                return {"logged_in": account_id == "acc_one", "credit": {"user_id": 1}}

            with mock.patch.object(module, "check_account_health", side_effect=fake_health):
                result = module.repair_saved_accounts()

        self.assertEqual(checked, ["acc_one"])
        self.assertEqual([item["account_id"] for item in result], ["acc_one"])

    def test_select_prepared_account_falls_back_after_preflight_failure(self):
        module = load_dreamina_module()
        now = time.time()
        with tempfile.TemporaryDirectory() as tmp:
            self.write_accounts(module, Path(tmp), {
                "accounts": [
                    {
                        "id": "acc_bad",
                        "name": "账号1",
                        "is_system_home": False,
                        "logged_in": True,
                        "_login_verified_at": now,
                    },
                    {
                        "id": "acc_default",
                        "name": "共享系统账号",
                        "is_system_home": True,
                        "logged_in": True,
                        "_login_verified_at": now,
                    },
                ],
                "active_account": "acc_bad",
                "dispatch_mode": "manual",
            })
            with mock.patch.object(module, "preflight_account_runtime", return_value={
                "ok": False,
                "error_code": "keychain_recovery_failed",
                "error_detail": "locked",
            }):
                selected = module.select_prepared_account_for_job()
            saved = module.load_accounts()["accounts"][0]

        self.assertTrue(selected["ok"])
        self.assertEqual(selected["account"]["id"], "acc_default")
        self.assertEqual(saved["last_error_code"], "keychain_recovery_failed")


if __name__ == "__main__":
    unittest.main()
