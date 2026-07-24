"""Verify Commit 2 dispatch changes (portal/app.py) preserve the exact
behavior of the pre-abstraction hardcoded logic.

Covers:
- KeyManager.add_key rejects volcengine-portrait provider (personal_key_disabled)
- KeyManager.add_key allows other providers (t8star/nano-banana/etc)
- credential_scheme wiring: api_key vs ak_sk vs none

Does NOT boot the HTTP server — imports portal.app and calls internal
helpers directly. Uses a temp state dir so it doesn't touch prod data.
"""
from __future__ import annotations

import importlib.util
import io
import os
import sys
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
PORTAL = ROOT / "portal"
if str(PORTAL) not in sys.path:
    sys.path.insert(0, str(PORTAL))


_ORIGINAL_DATA_DIR: str | None = None


def _load_portal_with_temp_state():
    """Load portal.app fresh with DATA_DIR pointing at a temp dir so
    KeyManager doesn't touch real state/user_keys.json.

    IMPORTANT: this mutates os.environ["DATA_DIR"] globally. tearDownClass
    restores it, and we force-reimport app.py after tests so other test
    modules see the restored env."""
    global _ORIGINAL_DATA_DIR
    _ORIGINAL_DATA_DIR = os.environ.get("DATA_DIR")
    tmp = tempfile.mkdtemp(prefix="portal-spec-test-")
    os.environ["DATA_DIR"] = tmp
    sys.modules.pop("app", None)
    spec = importlib.util.spec_from_file_location("app", PORTAL / "app.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module, tmp


def _restore_data_dir_env():
    """Undo the DATA_DIR mutation and drop the cached app module so later
    test files (e.g. test_output_layout) reload with the real env."""
    if _ORIGINAL_DATA_DIR is None:
        os.environ.pop("DATA_DIR", None)
    else:
        os.environ["DATA_DIR"] = _ORIGINAL_DATA_DIR
    sys.modules.pop("app", None)


class PersonalKeyDisabledTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod, cls.tmp = _load_portal_with_temp_state()

    @classmethod
    def tearDownClass(cls):
        _restore_data_dir_env()

    def test_volcengine_portrait_add_key_rejected(self):
        km = self.mod.KeyManager()
        with self.assertRaises(ValueError) as ctx:
            km.add_key("uid1", "test", "volcengine-portrait", "some-key", "")
        # Message format changed to include display_name, but Chinese
        # phrase "admin 统一配置" is preserved for UX continuity.
        self.assertIn("admin 统一配置", str(ctx.exception))

    def test_t8star_add_key_allowed(self):
        km = self.mod.KeyManager()
        entry = km.add_key("uid2", "my-t8", "t8star", "sk-test", "note")
        self.assertEqual(entry["provider"], "t8star")

    def test_gemini_add_key_allowed(self):
        # gemini has no spec -> personal_key_disabled gate should skip
        km = self.mod.KeyManager()
        entry = km.add_key("uid3", "test", "gemini", "sk-gemini", "")
        self.assertEqual(entry["provider"], "gemini")


class CredentialSchemeSpecTests(unittest.TestCase):
    """Contract check on spec.credential_scheme values — dispatch code in
    _proxy reads these strings, so the JSON must produce exactly the right
    literal for each app."""

    @classmethod
    def setUpClass(cls):
        from app_spec import load_specs
        specs = load_specs(PORTAL / "apps.json", ROOT)
        cls.by_name = {s.name: s for s in specs}

    def test_seedance_is_api_key(self):
        self.assertEqual(self.by_name["seedance"].credential_scheme, "api_key")

    def test_nano_banana_is_api_key(self):
        self.assertEqual(self.by_name["nano-banana"].credential_scheme, "api_key")

    def test_dreamina_is_none(self):
        # dreamina uses account cookies (X-Dreamina-Manage), no per-request key
        self.assertEqual(self.by_name["dreamina"].credential_scheme, "none")

    def test_volcengine_portrait_is_ak_sk(self):
        self.assertEqual(self.by_name["volcengine-portrait"].credential_scheme, "ak_sk")


class ProxyIdentityHeadersTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod, cls.tmp = _load_portal_with_temp_state()

    @classmethod
    def tearDownClass(cls):
        _restore_data_dir_env()

    def test_proxy_uses_session_user_identity_not_forged_browser_header(self):
        upstream_headers = {}

        class FakeResponse(io.BytesIO):
            status = 200

            def getheader(self, name, default=None):
                return default

            def getheaders(self):
                return []

        class FakeConnection:
            def __init__(self, host, port, timeout):
                self.response = FakeResponse()

            def request(self, method, path, body=None, headers=None):
                upstream_headers.update(headers or {})

            def getresponse(self):
                return self.response

            def close(self):
                pass

        handler = self.mod.Handler.__new__(self.mod.Handler)
        handler.client_address = ("127.0.0.1", 12345)
        handler.headers = Message()
        handler.headers["X-Portal-User-Id"] = "attacker"
        handler.rfile = io.BytesIO()
        handler.wfile = io.BytesIO()
        handler._is_https = lambda: False
        handler.send_response = lambda status: None
        handler.send_header = lambda key, value: None
        handler._cors_headers = lambda: None
        handler.end_headers = lambda: None

        user = {"user_id": "user-a-immutable", "username": "测试用户", "role": "user"}
        with patch.object(self.mod.http.client, "HTTPConnection", FakeConnection):
            handler._proxy("feishu-generation-agent", 8765, "GET", "/", user)

        self.assertEqual(upstream_headers["X-Portal-User-Id"], "user-a-immutable")
        self.assertEqual(upstream_headers["X-Username"], "%E6%B5%8B%E8%AF%95%E7%94%A8%E6%88%B7")
        self.assertNotEqual(upstream_headers["X-Portal-User-Id"], "attacker")
        ts = int(upstream_headers["X-Portal-Ts"])
        self.assertEqual(
            upstream_headers["X-Portal-Sig"],
            self.mod._sign_admin_header(upstream_headers["X-Username"], False, ts),
        )

    def test_proxy_forwards_put_and_patch_request_bodies(self):
        payload = b'{"prompt_text":"Chinese plan"}'
        forwarded = []

        class FakeResponse(io.BytesIO):
            status = 200

            def getheader(self, name, default=None):
                return default

            def getheaders(self):
                return []

        class FakeConnection:
            def __init__(self, host, port, timeout):
                self.response = FakeResponse()

            def request(self, method, path, body=None, headers=None):
                forwarded.append((method, body, headers))

            def getresponse(self):
                return self.response

            def close(self):
                pass

        user = {
            "user_id": "user-a-immutable",
            "username": "测试用户",
            "role": "user",
        }
        for method in ("PUT", "PATCH"):
            with self.subTest(method=method):
                handler = self.mod.Handler.__new__(self.mod.Handler)
                handler.client_address = ("127.0.0.1", 12345)
                handler.headers = Message()
                handler.headers["Content-Type"] = "application/json"
                handler.headers["Content-Length"] = str(len(payload))
                handler.rfile = io.BytesIO(payload)
                handler.wfile = io.BytesIO()
                handler._is_https = lambda: False
                handler.send_response = lambda status: None
                handler.send_header = lambda key, value: None
                handler._cors_headers = lambda: None
                handler.end_headers = lambda: None

                with patch.object(
                    self.mod.http.client,
                    "HTTPConnection",
                    FakeConnection,
                ):
                    handler._proxy(
                        "feishu-generation-agent",
                        8765,
                        method,
                        "/api/planner-prompt",
                        user,
                    )

        self.assertEqual(
            [(method, body) for method, body, _ in forwarded],
            [("PUT", payload), ("PATCH", payload)],
        )


class ProxyHttpMethodDispatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod, cls.tmp = _load_portal_with_temp_state()

    @classmethod
    def tearDownClass(cls):
        _restore_data_dir_env()

    def test_authenticated_agent_mutation_methods_are_proxied(self):
        user = {
            "user_id": "user-a-immutable",
            "username": "测试用户",
            "role": "user",
        }
        for method in ("PUT", "PATCH", "DELETE"):
            with self.subTest(method=method):
                handler = self.mod.Handler.__new__(self.mod.Handler)
                handler.path = "/feishu-generation-agent/api/planner-prompt"
                handler._reject_oversized_upload = lambda: False
                handler._require_auth = lambda path: user
                proxied = []
                handler._try_proxy = (
                    lambda path, actual_method, actual_user: proxied.append(
                        (path, actual_method, actual_user)
                    )
                    or True
                )
                handler._json = lambda status, payload: self.fail(
                    f"unexpected response {status}: {payload}"
                )

                dispatch = getattr(handler, f"do_{method}", None)
                self.assertIsNotNone(
                    dispatch,
                    f"Portal does not implement HTTP {method} dispatch",
                )
                if dispatch is None:
                    continue
                dispatch()

                self.assertEqual(
                    proxied,
                    [
                        (
                            "/feishu-generation-agent/api/planner-prompt",
                            method,
                            user,
                        )
                    ],
                )


class FeishuAgentNavigationTests(unittest.TestCase):
    def test_feishu_agent_tab_uses_registered_relative_iframe_url(self):
        html = (PORTAL / "static" / "index.html").read_text(encoding="utf-8")
        js = (PORTAL / "static" / "app.js").read_text(encoding="utf-8")

        self.assertIn('data-tab="feishu-generation-agent"', html)
        self.assertIn('data-app="feishu-generation-agent"', html)
        self.assertIn("iframe_url", js)
        for literal_host in ("192.168.30.5", "localhost:8765", "127.0.0.1:8765"):
            self.assertNotIn(literal_host, html)
            self.assertNotIn(literal_host, js)


if __name__ == "__main__":
    unittest.main()
