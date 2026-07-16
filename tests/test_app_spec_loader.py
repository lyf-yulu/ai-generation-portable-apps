"""AppSpec loader and dispatch helpers — verify the L2 abstraction reads
apps.json correctly and preserves golden set ordering + shape.

Regression coverage:
- tab order (seedance → nano-banana → dreamina → volcengine-portrait) must
  match legacy hardcoded order so stats tables don't shift columns
- credential_scheme and feature flags must decode to the right types
- classify_job_type must match the legacy _proxy logic (portal/app.py 1751-1757)
- resolve_extra_headers must expand {perm:xxx} placeholders
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
PORTAL = ROOT / "portal"
if str(PORTAL) not in sys.path:
    sys.path.insert(0, str(PORTAL))

from app_spec import (
    AppSpec,
    classify_job_type,
    load_specs,
    resolve_extra_headers,
)


class AppSpecLoaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.specs = load_specs(PORTAL / "apps.json", ROOT)
        cls.by_name = {s.name: s for s in cls.specs}

    def test_tab_order_preserves_legacy_layout(self):
        # Stats tables in static/index.html assume this exact order.
        expected_head = ["seedance", "nano-banana", "dreamina", "volcengine-portrait"]
        self.assertEqual([s.name for s in self.specs][:4], expected_head)

    def test_golden_set_is_exactly_four(self):
        # Production apps.json holds only the 4 golden apps. hello-world is a
        # reference sub-app kept in the repo for the 9190 canary but NOT
        # registered in production (its stdlib launch tripped port-cleanup's
        # orphan killer — see portal/app.py _kill_port_squatter).
        self.assertEqual(
            [s.name for s in self.specs],
            ["seedance", "nano-banana", "dreamina", "volcengine-portrait"],
        )

    def test_no_duplicate_names(self):
        names = [s.name for s in self.specs]
        self.assertEqual(len(names), len(set(names)))

    def test_seedance_capabilities(self):
        s = self.by_name["seedance"]
        self.assertTrue(s.needs_tos_creds)
        self.assertFalse(s.is_tos_source)
        self.assertEqual(s.credential_scheme, "api_key")
        self.assertEqual(s.job_type, "video")
        self.assertEqual(s.mount, "iframe")
        self.assertEqual(s.port_default, 8787)
        self.assertIsNone(s.admin_permission)
        self.assertEqual(s.extra_headers, {})

    def test_nano_banana_capabilities(self):
        s = self.by_name["nano-banana"]
        self.assertFalse(s.needs_tos_creds)
        self.assertEqual(s.credential_scheme, "api_key")
        self.assertEqual(s.job_type, "image")
        self.assertEqual(s.port_default, 8797)

    def test_dreamina_capabilities(self):
        s = self.by_name["dreamina"]
        self.assertEqual(s.admin_permission, "manage_dreamina_accounts")
        self.assertEqual(s.extra_headers, {"X-Dreamina-Manage": "{perm:use_apps}"})
        self.assertEqual(s.credential_scheme, "none")
        self.assertEqual(s.job_type, "dynamic")
        self.assertEqual(s.mount, "component")
        self.assertEqual(s.component_factory, "DreaminaApp")
        self.assertEqual(len(s.job_type_rules), 1)
        rule = s.job_type_rules[0]
        self.assertEqual(rule.type, "image")
        self.assertIn("text2image", rule.keywords)
        self.assertIn("image2image", rule.keywords)

    def test_volcengine_portrait_capabilities(self):
        s = self.by_name["volcengine-portrait"]
        self.assertTrue(s.personal_key_disabled)
        self.assertEqual(s.credential_scheme, "ak_sk")
        self.assertEqual(s.company_key_endpoint, "portrait-key")
        self.assertTrue(s.is_tos_source)
        self.assertTrue(s.needs_tos_creds)
        self.assertEqual(s.job_type, "video")
        self.assertEqual(s.display_name, "人像生成")
        self.assertEqual(s.component_factory, "VolcenginePortraitApp")

    def test_minimal_iframe_app_defaults(self):
        # Loader must decode a minimal iframe app (all optional flags omitted)
        # to safe defaults. Uses a temp fixture, not production apps.json —
        # hello-world is no longer registered in prod but stays as the
        # canonical "simplest sub-app" shape a new app author copies.
        import json
        import tempfile
        fixture = [{
            "name": "demo-echo",
            "display_name": "Demo",
            "port_env": "DEMO_PORT",
            "port_default": 8899,
            "mount": "iframe",
            "iframe_url": "/demo-echo/index.html",
            "color": "#6366f1",
            "credential_scheme": "none",
            "job_type": "image",
            "metrics": ["images"],
            "unit_label": "次",
        }]
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(fixture, f)
            fpath = Path(f.name)
        try:
            specs = load_specs(fpath, ROOT)
            s = {sp.name: sp for sp in specs}["demo-echo"]
            self.assertEqual(s.mount, "iframe")
            self.assertEqual(s.credential_scheme, "none")
            self.assertFalse(s.needs_tos_creds)
            self.assertFalse(s.personal_key_disabled)
            self.assertIsNone(s.admin_permission)
            self.assertIsNone(s.company_key_endpoint)
            self.assertEqual(s.job_type, "image")
            self.assertEqual(s.extra_headers, {})
        finally:
            fpath.unlink()

    def test_dir_path_is_absolute(self):
        for s in self.specs:
            self.assertTrue(s.dir_path.is_absolute(), s.name)
            self.assertEqual(s.dir_path, ROOT / s.name)

    def test_port_reads_env_override(self):
        import os
        s = self.by_name["seedance"]
        # No env override -> default
        os.environ.pop("SEEDANCE_PORT", None)
        self.assertEqual(s.port, 8787)
        # With env override
        os.environ["SEEDANCE_PORT"] = "8788"
        try:
            self.assertEqual(s.port, 8788)
        finally:
            os.environ.pop("SEEDANCE_PORT", None)


class ClassifyJobTypeTests(unittest.TestCase):
    """Verify classify_job_type() matches the legacy hardcoded logic at
    portal/app.py:1751-1757 byte-for-byte."""

    @classmethod
    def setUpClass(cls):
        specs = load_specs(PORTAL / "apps.json", ROOT)
        cls.by_name = {s.name: s for s in specs}

    def test_seedance_always_video(self):
        s = self.by_name["seedance"]
        self.assertEqual(classify_job_type(s, "/api/jobs"), "video")
        self.assertEqual(classify_job_type(s, "/api/whatever"), "video")

    def test_volcengine_portrait_always_video(self):
        s = self.by_name["volcengine-portrait"]
        self.assertEqual(classify_job_type(s, "/api/jobs"), "video")

    def test_nano_banana_always_image(self):
        s = self.by_name["nano-banana"]
        self.assertEqual(classify_job_type(s, "/api/jobs"), "image")

    def test_dreamina_text2image_is_image(self):
        s = self.by_name["dreamina"]
        self.assertEqual(classify_job_type(s, "/api/text2image"), "image")

    def test_dreamina_image2image_is_image(self):
        s = self.by_name["dreamina"]
        self.assertEqual(classify_job_type(s, "/api/image2image"), "image")

    def test_dreamina_frames2video_is_video(self):
        s = self.by_name["dreamina"]
        self.assertEqual(classify_job_type(s, "/api/frames2video"), "video")

    def test_dreamina_generic_video_is_video(self):
        s = self.by_name["dreamina"]
        self.assertEqual(classify_job_type(s, "/api/video-generate"), "video")
        # (a static job_type="image" app is covered by test_nano_banana_always_image)


class ExtraHeadersTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        specs = load_specs(PORTAL / "apps.json", ROOT)
        cls.dreamina = next(s for s in specs if s.name == "dreamina")
        cls.seedance = next(s for s in specs if s.name == "seedance")

    @staticmethod
    def _has_permission(user, perm):
        return perm in user.get("perms", set())

    def test_dreamina_use_apps_gets_manage_header(self):
        user = {"perms": {"use_apps"}}
        headers = resolve_extra_headers(self.dreamina, user, self._has_permission)
        self.assertEqual(headers, {"X-Dreamina-Manage": "1"})

    def test_dreamina_no_use_apps_drops_header(self):
        user = {"perms": set()}
        headers = resolve_extra_headers(self.dreamina, user, self._has_permission)
        self.assertEqual(headers, {})

    def test_seedance_no_extra_headers(self):
        user = {"perms": {"use_apps"}}
        headers = resolve_extra_headers(self.seedance, user, self._has_permission)
        self.assertEqual(headers, {})


if __name__ == "__main__":
    unittest.main()
