"""Verify /api/platform/portrait-key still routes correctly after
the L2 abstraction (Commit 3). Also verify unknown platform endpoints
fall through, and that _company_key_get keeps the strict role=='admin'
check (does NOT go through spec.admin_permission)."""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PORTAL = ROOT / "portal"
if str(PORTAL) not in sys.path:
    sys.path.insert(0, str(PORTAL))


def _load_portal_fresh():
    tmp = tempfile.mkdtemp(prefix="portal-ck-test-")
    orig = os.environ.get("DATA_DIR")
    os.environ["DATA_DIR"] = tmp
    sys.modules.pop("app", None)
    spec = importlib.util.spec_from_file_location("app", PORTAL / "app.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module, tmp, orig


class CompanyKeyRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod, cls.tmp, cls._orig_data_dir = _load_portal_fresh()

    @classmethod
    def tearDownClass(cls):
        if cls._orig_data_dir is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = cls._orig_data_dir
        sys.modules.pop("app", None)

    def _make_handler(self):
        """Build a minimal Handler stub that only cares about response
        capture. Avoids booting the ThreadingHTTPServer."""
        h = self.mod.Handler.__new__(self.mod.Handler)
        h._json_captures = []

        def _json(status, payload):
            h._json_captures.append((status, payload))

        h._json = _json
        h._read_json = lambda: None
        return h

    def test_portrait_key_get_routes_to_volcengine_portrait_spec(self):
        h = self._make_handler()
        called = {}
        # Instance attr overrides bound method — the wrapper calls it as
        # self._company_key_get(spec, user) which after Python's attribute
        # lookup binds via the class, but instance-level lambdas don't get
        # `self` prepended (they're stored as plain attributes, not descriptors).
        h._company_key_get = lambda spec, user, called=called: called.update(
            spec_name=spec.name, user_role=user.get("role")
        )
        h._company_key_set = lambda spec, user: None
        user = {"role": "admin", "username": "root"}
        result = h._try_company_key_route("/api/platform/portrait-key", "GET", user)
        self.assertTrue(result)
        self.assertEqual(called["spec_name"], "volcengine-portrait")
        self.assertEqual(called["user_role"], "admin")

    def test_portrait_key_post_routes_to_set(self):
        h = self._make_handler()
        called = {}
        h._company_key_get = lambda spec, user: None
        h._company_key_set = lambda spec, user, called=called: called.update(
            method="set", spec_name=spec.name
        )
        result = h._try_company_key_route("/api/platform/portrait-key", "POST", {"role": "admin"})
        self.assertTrue(result)
        self.assertEqual(called["method"], "set")
        self.assertEqual(called["spec_name"], "volcengine-portrait")

    def test_unknown_platform_endpoint_falls_through(self):
        h = self._make_handler()
        h._company_key_get = lambda spec, user: None
        h._company_key_set = lambda spec, user: None
        result = h._try_company_key_route("/api/platform/nonexistent", "GET", {"role": "admin"})
        self.assertFalse(result)

    def test_non_platform_path_falls_through(self):
        h = self._make_handler()
        result = h._try_company_key_route("/api/whatever", "GET", {"role": "admin"})
        self.assertFalse(result)

    def test_bad_method_returns_405(self):
        h = self._make_handler()
        h._company_key_get = lambda spec, user: None
        h._company_key_set = lambda spec, user: None
        result = h._try_company_key_route("/api/platform/portrait-key", "DELETE", {"role": "admin"})
        self.assertTrue(result)
        self.assertEqual(h._json_captures[-1][0], 405)

    def test_company_key_get_rejects_non_admin(self):
        # This is the SECURITY-CRITICAL test: even if a user has
        # manage_dreamina_accounts (which _proxy would grant X-Is-Admin for),
        # _company_key_get must reject them because role != "admin".
        h = self._make_handler()
        spec = self.mod.SPEC_BY_NAME["volcengine-portrait"]
        user = {"role": "user", "username": "someone", "user_id": "u1"}
        h._company_key_get(spec, user)
        self.assertEqual(h._json_captures[-1][0], 403)
        self.assertIn("admin only", h._json_captures[-1][1].get("error", ""))


if __name__ == "__main__":
    unittest.main()
