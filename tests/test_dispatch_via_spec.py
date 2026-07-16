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
import os
import sys
import tempfile
import unittest
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main()
