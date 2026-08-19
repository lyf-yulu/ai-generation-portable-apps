"""Regression: in Portal mode (CORS=1) the sub-apps must IGNORE a client-supplied
output_dir and force outputs/<user>/<date>/, so remote users' custom paths can no
longer scatter results outside outputs/ (which hid them from the Feishu sync).
Standalone local mode (no CORS) keeps the custom-path ability.
"""
import importlib.util
import inspect
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(mod_path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, mod_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class TestNanoEnsureOutputDir:
    """nano-banana centralizes the logic in _ensure_output_dir(values, job_id)."""

    def setup_method(self):
        self.mod = _load(ROOT / "nano-banana" / "app.py", "nb_app_outlock")
        # register a fake job with a username
        with self.mod.LOCK:
            self.mod.JOBS["job1"] = {"username": "苏湘"}

    def teardown_method(self):
        os.environ.pop("CORS", None)
        with self.mod.LOCK:
            self.mod.JOBS.pop("job1", None)

    def test_portal_mode_overrides_custom_dir(self):
        os.environ["CORS"] = "1"
        values = {"output_dir": "/tmp/用户乱填的路径/【公交车】7.15"}
        self.mod._ensure_output_dir(values, "job1")
        # must be forced under OUTPUT_DIR/<user>/<date>, NOT the custom path
        assert "/tmp/用户乱填的路径" not in values["output_dir"]
        assert str(self.mod.OUTPUT_DIR) in values["output_dir"]
        assert "苏湘" in values["output_dir"]

    def test_portal_mode_empty_dir_still_user_subdir(self):
        os.environ["CORS"] = "1"
        values = {}
        self.mod._ensure_output_dir(values, "job1")
        assert str(self.mod.OUTPUT_DIR) in values["output_dir"]
        assert "苏湘" in values["output_dir"]

    def test_standalone_keeps_custom_dir(self):
        # no CORS => standalone local mode => custom path preserved
        os.environ.pop("CORS", None)
        values = {"output_dir": "/tmp/my-local-folder"}
        self.mod._ensure_output_dir(values, "job1")
        assert values["output_dir"] == "/tmp/my-local-folder"

    def test_standalone_empty_falls_back_to_user_subdir(self):
        os.environ.pop("CORS", None)
        values = {}
        self.mod._ensure_output_dir(values, "job1")
        assert str(self.mod.OUTPUT_DIR) in values["output_dir"]
        assert "苏湘" in values["output_dir"]


class TestLockLogicPresentInAllApps:
    """seedance & dreamina inline the same lock inside their job runner; assert
    the CORS gate is present so the three apps stay consistent."""

    def test_seedance_has_cors_lock(self):
        src = (ROOT / "seedance" / "app.py").read_text("utf-8")
        assert 'os.environ.get("CORS") == "1"' in src
        assert "_user_day_subdir(OUTPUT_DIR, username)" in src

    def test_dreamina_has_cors_lock(self):
        src = (ROOT / "dreamina" / "app.py").read_text("utf-8")
        assert 'os.environ.get("CORS") != "1"' in src
        assert "_user_day_subdir(OUTPUT_DIR, username)" in src
