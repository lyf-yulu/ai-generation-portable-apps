"""Tests for portrait purge helpers + handler."""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_portrait_app():
    mod_path = ROOT / "volcengine-portrait" / "app.py"
    spec = importlib.util.spec_from_file_location("portrait_app_for_purge_test", mod_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["portrait_app_for_purge_test"] = mod
    spec.loader.exec_module(mod)
    return mod


class TestParseGroupIdDate:
    def setup_method(self):
        self.mod = _load_portrait_app()

    def test_valid_id_returns_date(self):
        assert self.mod._parse_group_id_date("group-20260618175031-44ncn") == "2026-06-18"
        assert self.mod._parse_group_id_date("group-20260702000000-abcde") == "2026-07-02"

    def test_invalid_id_returns_none(self):
        assert self.mod._parse_group_id_date("我的珍藏") is None
        assert self.mod._parse_group_id_date("group-2026061-44ncn") is None
        assert self.mod._parse_group_id_date("group-abc-def") is None
        assert self.mod._parse_group_id_date("") is None
        assert self.mod._parse_group_id_date(None) is None

    def test_partial_matches_dont_leak(self):
        assert self.mod._parse_group_id_date("group-2026061817503144ncn") is None
