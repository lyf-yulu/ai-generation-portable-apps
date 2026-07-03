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


class FakeHandler:
    """Minimal stand-in for BaseHTTPRequestHandler used by handle_* functions."""
    def __init__(self, is_admin=True, body_bytes=b""):
        self.headers = {"X-Is-Admin": "1" if is_admin else "0",
                        "Content-Length": str(len(body_bytes))}
        self._body = body_bytes
        self.rfile = _FakeReader(body_bytes)
        self.status_code = None
        self.response_body = None
        self.path = "/api/virtual/groups/purge"

    def send_response(self, code):
        self.status_code = code

    def send_header(self, *a, **k): pass
    def end_headers(self): pass


class _FakeReader:
    def __init__(self, data): self._d = data
    def read(self, n=-1):
        if n < 0 or n >= len(self._d):
            out, self._d = self._d, b""
            return out
        out, self._d = self._d[:n], self._d[n:]
        return out


class _FakeWriter:
    def __init__(self): self.buf = b""
    def write(self, b): self.buf += b


def _install_writer(handler):
    handler.wfile = _FakeWriter()
    return handler.wfile


class TestPurgeHandler:
    def setup_method(self):
        self.mod = _load_portrait_app()

    def _post(self, is_admin=True, body_dict=None):
        import json
        body = json.dumps(body_dict or {}).encode()
        h = FakeHandler(is_admin=is_admin, body_bytes=body)
        writer = _install_writer(h)
        self.mod.handle_virtual_groups_purge(h)
        return h.status_code, (json.loads(writer.buf.decode()) if writer.buf else {})

    def test_non_admin_forbidden(self):
        code, resp = self._post(is_admin=False, body_dict={"before_date": "2026-07-02"})
        assert code == 403
        assert resp.get("ok") is False

    def test_missing_before_date(self):
        code, _ = self._post(body_dict={})
        assert code == 400

    def test_invalid_before_date_format(self):
        for bad in ["2026-7-2", "20260702", "", "yesterday"]:
            code, _ = self._post(body_dict={"before_date": bad})
            assert code == 400, bad

    def test_before_date_too_old(self):
        code, _ = self._post(body_dict={"before_date": "1999-01-01"})
        assert code == 400

    def test_before_date_too_far_future(self):
        from datetime import date, timedelta
        far = (date.today() + timedelta(days=30)).strftime("%Y-%m-%d")
        code, _ = self._post(body_dict={"before_date": far})
        assert code == 400

    def test_dry_run_filters_by_id_date(self, monkeypatch):
        page_items = [
            {"Id": "group-20260618175031-abcde", "Name": "old"},
            {"Id": "group-20260701090000-fghij", "Name": "cutoff-day"},
            {"Id": "group-20260630120000-klmno", "Name": "before-cutoff"},
            {"Id": "我的珍藏", "Name": "手动组"},
        ]

        def fake_openapi(action, body, ak=None, sk=None):
            if action == "ListAssetGroups":
                return {"result": {"Items": page_items, "TotalCount": 4}}
            if action == "ListAssets":
                return {"result": {"Items": [], "TotalCount": 0}}
            return {"error": "unexpected"}

        monkeypatch.setattr(self.mod, "openapi_call", fake_openapi)
        code, resp = self._post(body_dict={"before_date": "2026-07-01", "dry_run": True})
        assert code == 200, resp
        assert resp["ok"] is True
        assert resp["dry_run"] is True
        assert resp["total_scanned"] == 4
        assert resp["matched"] == 2
        assert resp["skipped_non_matching_id"] == 1
        got_ids = [c["group_id"] for c in resp["candidates"]]
        assert "group-20260618175031-abcde" in got_ids
        assert "group-20260630120000-klmno" in got_ids
        assert "group-20260701090000-fghij" not in got_ids

    def test_cap_at_200(self, monkeypatch):
        # 14-digit YYYYMMDDHHMMSS: 20260618 + HHMMSS from index
        many = [{"Id": f"group-20260618{i:06d}-abcde", "Name": "x"} for i in range(210)]

        def fake_openapi(action, body, ak=None, sk=None):
            if action == "ListAssetGroups":
                return {"result": {"Items": many, "TotalCount": 210}}
            return {"error": "unexpected"}

        monkeypatch.setattr(self.mod, "openapi_call", fake_openapi)
        code, resp = self._post(body_dict={"before_date": "2026-07-01", "dry_run": True})
        assert code == 400
        assert "200" in (resp.get("error") or "").lower() or "batch" in (resp.get("error") or "").lower()
