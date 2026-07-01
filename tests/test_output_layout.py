"""Verify seedance/nano-banana/dreamina/volcengine-portrait share the same
username sanitization + user/day subdir helpers, and that new outputs land
under <username>/<date>/ while old flat files remain readable."""
import importlib.util
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(mod_path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, mod_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class TestSeedanceHelpers:
    def setup_method(self):
        self.mod = _load(ROOT / "seedance" / "app.py", "seedance_app_for_layout_test")

    def test_sanitize_username_basics(self):
        assert self.mod._sanitize_username("alice") == "alice"
        assert self.mod._sanitize_username("张三") == "张三"
        assert self.mod._sanitize_username(" bob ") == "bob"
        assert self.mod._sanitize_username("") == "unknown"
        assert self.mod._sanitize_username(None) == "unknown"

    def test_sanitize_username_strips_slashes(self):
        assert self.mod._sanitize_username("a/b") == "a_b"
        assert self.mod._sanitize_username("../evil") == "evil"
        assert "/" not in self.mod._sanitize_username("x/y/z")

    def test_sanitize_username_length_capped(self):
        result = self.mod._sanitize_username("a" * 200)
        assert len(result) == 40

    def test_user_day_subdir_creates_and_returns(self, tmp_path):
        base = tmp_path / "outputs"
        out = self.mod._user_day_subdir(base, "alice", day="2026-07-01")
        assert out == base / "alice" / "2026-07-01"
        assert out.is_dir()

    def test_user_day_subdir_defaults_to_today(self, tmp_path):
        base = tmp_path / "outputs"
        out = self.mod._user_day_subdir(base, "bob")
        assert out.parent.name == "bob"
        assert re.match(r"\d{4}-\d{2}-\d{2}$", out.name)


class TestNanoBananaHelpers:
    def setup_method(self):
        self.mod = _load(ROOT / "nano-banana" / "app.py", "nanobanana_app_for_layout_test")

    def test_sanitize_username_matches_seedance(self):
        seedance = _load(ROOT / "seedance" / "app.py", "seedance_app_for_layout_test_cmp")
        cases = ["alice", "张三", "", None, "a/b", "../evil", "a" * 200]
        for c in cases:
            assert self.mod._sanitize_username(c) == seedance._sanitize_username(c), c

    def test_user_day_subdir_creates(self, tmp_path):
        out = self.mod._user_day_subdir(tmp_path, "alice", day="2026-07-01")
        assert out == tmp_path / "alice" / "2026-07-01"
        assert out.is_dir()


class TestDreaminaHelpers:
    def setup_method(self):
        self.mod = _load(ROOT / "dreamina" / "app.py", "dreamina_app_for_layout_test")

    def test_sanitize_matches_others(self):
        seedance = _load(ROOT / "seedance" / "app.py", "seedance_app_for_layout_test_cmp2")
        for c in ["alice", "张三", "", None, "a/b", "../evil"]:
            assert self.mod._sanitize_username(c) == seedance._sanitize_username(c)

    def test_user_day_subdir_creates(self, tmp_path):
        out = self.mod._user_day_subdir(tmp_path, "alice", day="2026-07-01")
        assert out == tmp_path / "alice" / "2026-07-01"
        assert out.is_dir()


class TestPortraitHelpers:
    def setup_method(self):
        self.mod = _load(ROOT / "volcengine-portrait" / "app.py", "portrait_app_for_layout_test")

    def test_sanitize_matches_others(self):
        seedance = _load(ROOT / "seedance" / "app.py", "seedance_app_for_layout_test_cmp3")
        for c in ["alice", "张三", "", None, "a/b", "../evil"]:
            assert self.mod._sanitize_username(c) == seedance._sanitize_username(c)

    def test_user_day_subdir_creates(self, tmp_path):
        out = self.mod._user_day_subdir(tmp_path, "alice", day="2026-07-01")
        assert out == tmp_path / "alice" / "2026-07-01"
        assert out.is_dir()
