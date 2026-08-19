"""Scanner: correct four-dimension parsing + skip rules, all on a temp tree."""
import os
import time

from scanner import Artifact, scan


def _make(base, app, user, date, name, content=b"x", age=3600):
    d = base / app / "outputs" / user / date
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.write_bytes(content)
    old = time.time() - age
    os.utime(f, (old, old))
    return f


def _roots(base):
    return {
        "seedance": base / "seedance" / "outputs",
        "nano-banana": base / "nano-banana" / "outputs",
    }


def test_parses_four_dimensions(tmp_path):
    _make(tmp_path, "seedance", "苏湘", "2026-07-21", "20260721_1_run1_x.mp4")
    arts = scan(_roots(tmp_path))
    assert len(arts) == 1
    a = arts[0]
    assert a.app == "seedance"
    assert a.user == "苏湘"
    assert a.date == "2026-07-21"
    assert a.filename == "20260721_1_run1_x.mp4"
    assert a.size == 1


def test_multiple_apps_users(tmp_path):
    _make(tmp_path, "seedance", "苏湘", "2026-07-21", "a.mp4")
    _make(tmp_path, "nano-banana", "高大王", "2026-07-20", "b.png")
    arts = scan(_roots(tmp_path))
    apps = {a.app for a in arts}
    users = {a.user for a in arts}
    assert apps == {"seedance", "nano-banana"}
    assert users == {"苏湘", "高大王"}


def test_skips_non_media(tmp_path):
    _make(tmp_path, "seedance", "u", "2026-07-21", "note.txt")
    _make(tmp_path, "seedance", "u", "2026-07-21", ".DS_Store")
    _make(tmp_path, "seedance", "u", "2026-07-21", "meta.json")
    assert scan(_roots(tmp_path)) == []


def test_skips_zero_byte(tmp_path):
    _make(tmp_path, "seedance", "u", "2026-07-21", "empty.png", content=b"")
    assert scan(_roots(tmp_path)) == []


def test_skips_freshly_written(tmp_path):
    # age < MIN_AGE_SECONDS => still being written => skipped this round
    _make(tmp_path, "seedance", "u", "2026-07-21", "fresh.mp4", age=2)
    assert scan(_roots(tmp_path)) == []


def test_skips_non_date_dir(tmp_path):
    d = tmp_path / "seedance" / "outputs" / "u" / "notadate"
    d.mkdir(parents=True)
    (d / "x.png").write_bytes(b"x")
    assert scan(_roots(tmp_path)) == []


def test_missing_root_is_ignored(tmp_path):
    # nano-banana outputs never created; must not raise
    _make(tmp_path, "seedance", "u", "2026-07-21", "a.png")
    arts = scan(_roots(tmp_path))
    assert len(arts) == 1


def test_sorted_oldest_first(tmp_path):
    _make(tmp_path, "seedance", "u", "2026-07-21", "new.png", age=10)
    _make(tmp_path, "seedance", "u", "2026-07-21", "old.png", age=99999)
    arts = scan(_roots(tmp_path))
    assert [a.filename for a in arts] == ["old.png", "new.png"]


def test_fields_shape(tmp_path):
    _make(tmp_path, "seedance", "苏湘", "2026-07-21", "x.mp4")
    a = scan(_roots(tmp_path))[0]
    fields = a.fields()
    assert fields["文件名"] == "x.mp4"
    assert fields["日期"] == "2026-07-21"
    assert fields["子应用"] == "seedance"
    assert "生成时间" in fields
