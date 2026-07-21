"""Regression for #15/#17: downloads that fail mid-transfer with
http.client.IncompleteRead must be retried, not surfaced raw.

IncompleteRead subclasses HTTPException (not URLError/TimeoutError/OSError),
so the existing except clauses missed it and the raw exception reached the
user's job errors even though generation had succeeded. These tests confirm
the download helpers now retry on IncompleteRead and only raise a friendly
RuntimeError after exhausting attempts.
"""
import http.client
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load(mod_path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, mod_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class _FakeResp:
    """Context-manager stand-in for urlopen()'s response."""
    def __init__(self, data: bytes, ctype: str = "image/png"):
        self._data = data
        self._ctype = ctype

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._data

    class _Headers:
        def __init__(self, ctype):
            self._ctype = ctype

        def get_content_type(self):
            return self._ctype

    @property
    def headers(self):
        return _FakeResp._Headers(self._ctype)


def _make_urlopen(fail_times: int, payload: bytes):
    """Return a fake urlopen that raises IncompleteRead `fail_times` times,
    then yields a good response."""
    state = {"calls": 0}

    def fake_urlopen(req, timeout=None, **kw):
        state["calls"] += 1
        if state["calls"] <= fail_times:
            raise http.client.IncompleteRead(partial=payload[:3], expected=len(payload))
        return _FakeResp(payload)

    return fake_urlopen, state


class TestNanoDownloadUrl:
    def setup_method(self):
        self.mod = _load(ROOT / "nano-banana" / "app.py", "nb_app_incompleteread")

    def test_retries_then_succeeds(self, tmp_path, monkeypatch):
        monkeypatch.setattr(self.mod.time, "sleep", lambda *_: None)
        fake, state = _make_urlopen(fail_times=2, payload=b"IMAGEBYTES")
        monkeypatch.setattr(self.mod.urllib.request, "urlopen", fake)
        out = tmp_path / "x.png"
        self.mod.download_url("https://cdn/x.png", out)
        assert out.read_bytes() == b"IMAGEBYTES"
        assert state["calls"] == 3  # 2 failures + 1 success

    def test_raises_after_exhausting(self, tmp_path, monkeypatch):
        monkeypatch.setattr(self.mod.time, "sleep", lambda *_: None)
        fake, _ = _make_urlopen(fail_times=99, payload=b"IMAGEBYTES")
        monkeypatch.setattr(self.mod.urllib.request, "urlopen", fake)
        with pytest.raises(RuntimeError) as ei:
            self.mod.download_url("https://cdn/x.png", tmp_path / "x.png")
        assert "IncompleteRead" in str(ei.value)


class TestSeedanceDownloadVideo:
    def setup_method(self):
        self.mod = _load(ROOT / "seedance" / "app.py", "sd_app_incompleteread")

    def test_retries_then_succeeds(self, tmp_path, monkeypatch):
        monkeypatch.setattr(self.mod.time, "sleep", lambda *_: None)
        fake, state = _make_urlopen(fail_times=1, payload=b"VIDEOBYTES")
        monkeypatch.setattr(self.mod.urllib.request, "urlopen", fake)
        out = tmp_path / "v.mp4"
        self.mod.download_video("https://cdn/v.mp4", out)
        assert out.read_bytes() == b"VIDEOBYTES"
        assert state["calls"] == 2

    def test_raises_after_exhausting(self, tmp_path, monkeypatch):
        monkeypatch.setattr(self.mod.time, "sleep", lambda *_: None)
        fake, _ = _make_urlopen(fail_times=99, payload=b"VIDEOBYTES")
        monkeypatch.setattr(self.mod.urllib.request, "urlopen", fake)
        with pytest.raises(RuntimeError) as ei:
            self.mod.download_video("https://cdn/v.mp4", tmp_path / "v.mp4")
        assert "IncompleteRead" in str(ei.value)


class TestSeedanceMediaItemToFile:
    def setup_method(self):
        self.mod = _load(ROOT / "seedance" / "app.py", "sd_app_media_incompleteread")

    def test_reference_media_retries(self, monkeypatch):
        monkeypatch.setattr(self.mod.time, "sleep", lambda *_: None)
        fake, state = _make_urlopen(fail_times=2, payload=b"REFBYTES")
        monkeypatch.setattr(self.mod.urllib.request, "urlopen", fake)
        name, blob = self.mod.media_item_to_file("image", {"url": "https://cdn/r.png"})
        assert blob == b"REFBYTES"
        assert state["calls"] == 3
