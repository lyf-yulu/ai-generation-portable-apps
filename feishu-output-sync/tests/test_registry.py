"""Registry: idempotent fingerprints + persistent user->base mapping."""
import time
from pathlib import Path

from registry import Registry, fingerprint
from scanner import Artifact


def _art(tmp_path, name="x.mp4", size=100, mtime=1000.0):
    p = tmp_path / name
    p.write_bytes(b"x" * 1)
    return Artifact(app="seedance", user="苏湘", date="2026-07-21",
                    path=p, size=size, mtime=mtime)


def test_seen_false_then_true(tmp_path):
    reg = Registry(tmp_path / "state" / "db.sqlite3")
    art = _art(tmp_path)
    fp = fingerprint(art)
    assert reg.seen(fp) is False
    reg.mark(fp, art, "rec_1")
    assert reg.seen(fp) is True
    reg.close()


def test_mark_idempotent(tmp_path):
    reg = Registry(tmp_path / "state" / "db.sqlite3")
    art = _art(tmp_path)
    fp = fingerprint(art)
    reg.mark(fp, art, "rec_1")
    reg.mark(fp, art, "rec_2")  # INSERT OR REPLACE, no crash
    assert reg.seen(fp) is True
    reg.close()


def test_fingerprint_changes_with_size_or_mtime(tmp_path):
    a1 = _art(tmp_path, size=100, mtime=1000.0)
    a2 = _art(tmp_path, size=200, mtime=1000.0)
    a3 = _art(tmp_path, size=100, mtime=2000.0)
    assert fingerprint(a1) != fingerprint(a2)
    assert fingerprint(a1) != fingerprint(a3)


def test_fingerprint_stable_same_inputs(tmp_path):
    a1 = _art(tmp_path)
    a2 = _art(tmp_path)
    assert fingerprint(a1) == fingerprint(a2)


def test_user_base_roundtrip(tmp_path):
    reg = Registry(tmp_path / "state" / "db.sqlite3")
    assert reg.get_user_base("苏湘") is None
    tables = {"seedance": "tblA", "nano-banana": "tblB"}
    reg.save_user_base("苏湘", "app_tok", tables, authorized=True)
    got = reg.get_user_base("苏湘")
    assert got["app_token"] == "app_tok"
    assert got["table_ids"] == tables
    assert got["authorized"] is True
    reg.close()


def test_user_base_persists_across_reopen(tmp_path):
    db = tmp_path / "state" / "db.sqlite3"
    reg = Registry(db)
    reg.save_user_base("高大王", "tok2", {"seedance": "t1"}, authorized=True)
    reg.close()

    reg2 = Registry(db)
    got = reg2.get_user_base("高大王")
    assert got is not None
    assert got["app_token"] == "tok2"
    reg2.close()


def test_synced_persists_across_reopen(tmp_path):
    db = tmp_path / "state" / "db.sqlite3"
    reg = Registry(db)
    art = _art(tmp_path)
    fp = fingerprint(art)
    reg.mark(fp, art, "rec")
    reg.close()

    reg2 = Registry(db)
    assert reg2.seen(fp) is True
    reg2.close()
