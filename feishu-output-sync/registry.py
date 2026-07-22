"""SQLite state for the sync: what has been uploaded, and each user's bitable.

Two tables:
  synced     — one row per uploaded artifact, keyed by a content fingerprint so
               re-scans / restarts never re-upload the same file (idempotent).
  user_base  — maps a username to their auto-created bitable App + sub-app
               tables, so we build each user's base once and reuse it.

Pure stdlib sqlite3. The DB lives under state/ (gitignored).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path

from scanner import Artifact


def fingerprint(art: Artifact, root: Path | None = None) -> str:
    """Stable id for an artifact: relative-path + size + mtime.

    Using path+size+mtime (not file contents) keeps scanning cheap — we never
    read multi-hundred-MB videos just to decide if they changed. A regenerated
    file with the same name lands in a new timestamped path anyway, so path
    already carries most of the identity; size+mtime guard against edge reuse.
    """
    try:
        rel = str(art.path.relative_to(root)) if root else str(art.path)
    except ValueError:
        rel = str(art.path)
    raw = f"{rel}|{art.size}|{int(art.mtime)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


class Registry:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False so a future threaded loop can share it; the
        # sync loop is single-threaded today.
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS synced (
                fingerprint      TEXT PRIMARY KEY,
                app              TEXT NOT NULL,
                user             TEXT NOT NULL,
                date             TEXT NOT NULL,
                filename         TEXT NOT NULL,
                feishu_record_id TEXT,
                uploaded_at      REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS user_base (
                user            TEXT PRIMARY KEY,
                app_token       TEXT NOT NULL,
                table_ids_json  TEXT NOT NULL,
                open_id         TEXT,
                authorized      INTEGER NOT NULL DEFAULT 0,
                created_at      REAL NOT NULL
            );
            """
        )
        self._conn.commit()

    # ----- synced artifacts -----

    def seen(self, fp: str) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM synced WHERE fingerprint = ? LIMIT 1", (fp,)
        )
        return cur.fetchone() is not None

    def mark(self, fp: str, art: Artifact, feishu_record_id: str | None) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO synced "
            "(fingerprint, app, user, date, filename, feishu_record_id, uploaded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (fp, art.app, art.user, art.date, art.filename,
             feishu_record_id, time.time()),
        )
        self._conn.commit()

    # ----- per-user bitable mapping -----

    def get_user_base(self, user: str) -> dict | None:
        cur = self._conn.execute(
            "SELECT app_token, table_ids_json, open_id, authorized "
            "FROM user_base WHERE user = ?",
            (user,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return {
            "app_token": row[0],
            "table_ids": json.loads(row[1]),
            "open_id": row[2],
            "authorized": bool(row[3]),
        }

    def save_user_base(
        self,
        user: str,
        app_token: str,
        table_ids: dict[str, str],
        authorized: bool,
        open_id: str | None = None,
    ) -> None:
        # open_id column retained for schema stability but no longer used —
        # bases are shared org-wide instead of per-user granted.
        self._conn.execute(
            "INSERT OR REPLACE INTO user_base "
            "(user, app_token, table_ids_json, open_id, authorized, created_at) "
            "VALUES (?, ?, ?, ?, ?, COALESCE("
            "  (SELECT created_at FROM user_base WHERE user = ?), ?))",
            (user, app_token, json.dumps(table_ids, ensure_ascii=False),
             open_id, 1 if authorized else 0, user, time.time()),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
