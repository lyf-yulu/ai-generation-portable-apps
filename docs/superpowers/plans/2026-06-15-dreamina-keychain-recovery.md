# Dreamina Keychain Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore the existing multi-HOME Dreamina account pool by adding bounded keychain preflight, account health checks, explicit failure states, and repair controls.

**Architecture:** Add account runtime helpers in `dreamina/app.py` that own saved-account preflight, keychain search-list dedupe, command timeout classification, and account state updates. Wire existing account refresh/login/task dispatch paths through the runtime, and add a small sidebar account panel in the Dreamina frontend.

**Tech Stack:** Python standard library HTTP server, macOS `security` CLI, Dreamina CLI, vanilla JavaScript/CSS, unittest.

---

### Task 1: Account Runtime Tests

**Files:**
- Modify: `tests/test_dreamina_accounts.py`

- [ ] **Step 1: Write failing tests for runtime behavior**

Add tests for missing homes, missing keychains, deduplicating project keychains, timeout classification, and successful health checks:

```python
def test_preflight_missing_home_reports_error(self):
    module = load_dreamina_module()
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        module.ACCOUNTS_DIR = base / "accounts"
        module.ACCOUNTS_DIR.mkdir()
        result = module.preflight_account_runtime("acc_missing")
    self.assertFalse(result["ok"])
    self.assertEqual(result["error_code"], "missing_home")


def test_preflight_missing_keychain_reports_error_on_macos(self):
    module = load_dreamina_module()
    with tempfile.TemporaryDirectory() as tmp, mock.patch.object(module.sys, "platform", "darwin"):
        base = Path(tmp)
        module.ACCOUNTS_DIR = base / "accounts"
        home = module.ACCOUNTS_DIR / "acc_one"
        home.mkdir(parents=True)
        result = module.preflight_account_runtime("acc_one")
    self.assertFalse(result["ok"])
    self.assertEqual(result["error_code"], "missing_keychain")


def test_preflight_deduplicates_project_keychain_entries(self):
    module = load_dreamina_module()
    commands = []
    with tempfile.TemporaryDirectory() as tmp, mock.patch.object(module.sys, "platform", "darwin"):
        base = Path(tmp)
        module.ACCOUNTS_DIR = base / "accounts"
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
    set_cmds = [cmd for cmd in commands if cmd[:4] == ["security", "list-keychains", "-d", "user"] and "-s" in cmd]
    self.assertEqual(set_cmds[-1].count(str(keychain)), 1)


def test_account_health_records_timeout(self):
    module = load_dreamina_module()
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        self.write_accounts(module, base, {
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
        base = Path(tmp)
        self.write_accounts(module, base, {
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
```

- [ ] **Step 2: Run tests to verify RED**

Run: `python3 tests/test_dreamina_accounts.py`

Expected: fails because `preflight_account_runtime` and `check_account_health` are missing.

### Task 2: Account Runtime Implementation

**Files:**
- Modify: `dreamina/app.py`

- [ ] **Step 1: Implement runtime helpers**

Add helpers after `get_account_by_id`:

```python
def project_account_home(account_id: str) -> Path:
    home = get_account_home(account_id).resolve()
    base = ACCOUNTS_DIR.resolve()
    if home != base and base in home.parents:
        return home
    raise ValueError("account home outside project accounts directory")
```

Implement `run_security`, `classify_account_error`, `preflight_account_runtime`, `apply_account_health`, and `check_account_health` using hard timeouts and normalized error codes.

- [ ] **Step 2: Run account tests**

Run: `python3 tests/test_dreamina_accounts.py`

Expected: all tests pass.

### Task 3: Wire Runtime Into Existing Account Paths

**Files:**
- Modify: `dreamina/app.py`

- [ ] **Step 1: Replace account login poll and refresh checks**

Update `handle_account_login_poll` and `handle_account_refresh` to call `check_account_health(acc_id)` instead of directly calling `check_login_with_env`.

- [ ] **Step 2: Add repair all endpoint**

Add `POST /api/accounts/repair-all`, local-only, that loops non-system accounts through `check_account_health`.

- [ ] **Step 3: Prepare account before task dispatch**

Add `prepare_account_for_job(account)` and use it in `handle_generate` and `handle_retry`. If selected saved account cannot preflight, mark it unavailable and fall back to another logged-in account or system account.

- [ ] **Step 4: Run tests**

Run:

```bash
python3 tests/test_dreamina_accounts.py
python3 -m py_compile dreamina/app.py
```

Expected: both pass.

### Task 4: Frontend Account Status Panel

**Files:**
- Modify: `dreamina/static/index.html`
- Modify: `dreamina/static/app.js`
- Modify: `dreamina/static/styles.css`

- [ ] **Step 1: Add account panel markup**

Add a sidebar section with `#accountPanel`, `#repairAllAccountsBtn`, and `#accountList`.

- [ ] **Step 2: Render account states**

Add `loadAccounts()`, `renderAccounts(data)`, `accountStatusLabel(account)`, and button handlers for Check and Repair. Call `loadAccounts()` from `enterMain()`.

- [ ] **Step 3: Add compact styling**

Style account rows so status, credit, and last error fit without overlapping.

- [ ] **Step 4: Run JS syntax check**

Run: `node --check dreamina/static/app.js`

Expected: passes.

### Task 5: Runtime Verification Against Local Dreamina Service

**Files:**
- No source changes expected.

- [ ] **Step 1: Restart app**

Run: `bash 'Start All.command'`

Expected: Portal starts and Dreamina is available on `http://127.0.0.1:8888`.

- [ ] **Step 2: Trigger repair all**

Run:

```bash
curl -sS -m 120 -X POST http://127.0.0.1:8888/api/accounts/repair-all | python3 -m json.tool
```

Expected: each saved account returns either online credit data or a specific error code; backend does not hang.

- [ ] **Step 3: Verify account API**

Run:

```bash
curl -sS -m 20 http://127.0.0.1:8888/api/accounts | python3 -m json.tool
```

Expected: accounts include `last_check_at` and explicit error fields.

### Task 6: Final Verification

**Files:**
- No source changes expected.

- [ ] **Step 1: Run full targeted verification**

Run:

```bash
python3 tests/test_dreamina_accounts.py
python3 tests/test_portal_startup.py
python3 tests/test_workspace_media.py
python3 -m py_compile dreamina/app.py portal/app.py seedance/app.py nano-banana/app.py
node --check dreamina/static/app.js
```

Expected: all pass.

- [ ] **Step 2: Inspect diff**

Run: `git diff -- dreamina/app.py dreamina/static/index.html dreamina/static/app.js dreamina/static/styles.css tests/test_dreamina_accounts.py`

Expected: diff only contains Dreamina keychain recovery changes and tests.
