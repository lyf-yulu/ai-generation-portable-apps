import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from feishu_generation_agent.domain import Artifact


_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  thread_id TEXT NOT NULL UNIQUE,
  source_url TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  node TEXT NOT NULL,
  status TEXT NOT NULL,
  summary TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS operations (
  run_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  operation TEXT NOT NULL,
  provider_id TEXT,
  status TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL,
  PRIMARY KEY (run_id, task_id, operation)
);
CREATE TABLE IF NOT EXISTS artifacts (
  artifact_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  artifact_json TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS vision_cache (
  cache_key TEXT PRIMARY KEY,
  description_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
"""

_BEARER_TOKEN = re.compile(r"(?i)(\bBearer\s+)[^\s,;]+")
_QUERY_TOKEN = re.compile(r"(?i)([?&]token=)[^&#\s]*")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _safe_summary(summary: str) -> str:
    redacted = _BEARER_TOKEN.sub(r"\1[REDACTED]", summary)
    redacted = _QUERY_TOKEN.sub(r"\1[REDACTED]", redacted)
    return redacted[:500]


class Repository:
    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection

    @classmethod
    async def open(cls, path: Path) -> "Repository":
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = await aiosqlite.connect(path)
        connection.row_factory = aiosqlite.Row
        try:
            await connection.executescript(_SCHEMA)
            await connection.commit()
        except BaseException:
            await connection.close()
            raise
        return cls(connection)

    async def close(self) -> None:
        await self._connection.close()

    async def create_run(
        self,
        run_id: str,
        thread_id: str,
        source_url: str,
        status: str = "pending",
    ) -> None:
        timestamp = _now()
        await self._connection.execute(
            """
            INSERT INTO runs (
              run_id, thread_id, source_url, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
              thread_id = excluded.thread_id,
              source_url = excluded.source_url,
              status = excluded.status,
              updated_at = excluded.updated_at
            """,
            (run_id, thread_id, source_url, status, timestamp, timestamp),
        )
        await self._connection.commit()

    async def append_event(
        self,
        run_id: str,
        node: str,
        status: str,
        summary: str,
    ) -> None:
        await self._connection.execute(
            """
            INSERT INTO events (run_id, node, status, summary, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (run_id, node, status, _safe_summary(summary), _now()),
        )
        await self._connection.commit()

    async def list_events(self, run_id: str) -> list[dict[str, Any]]:
        cursor = await self._connection.execute(
            """
            SELECT id, run_id, node, status, summary, created_at
            FROM events
            WHERE run_id = ?
            ORDER BY id ASC
            """,
            (run_id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]

    async def save_operation(
        self,
        run_id: str,
        task_id: str,
        operation: str,
        provider_id: str | None,
        status: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        payload_json = json.dumps(
            payload or {}, ensure_ascii=False, separators=(",", ":")
        )
        await self._connection.execute(
            """
            INSERT INTO operations (
              run_id, task_id, operation, provider_id, status,
              payload_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, task_id, operation) DO UPDATE SET
              provider_id = excluded.provider_id,
              status = excluded.status,
              payload_json = excluded.payload_json,
              updated_at = excluded.updated_at
            """,
            (
                run_id,
                task_id,
                operation,
                provider_id,
                status,
                payload_json,
                _now(),
            ),
        )
        await self._connection.commit()

    async def get_operation(
        self,
        run_id: str,
        task_id: str,
        operation: str,
    ) -> dict[str, Any] | None:
        cursor = await self._connection.execute(
            """
            SELECT run_id, task_id, operation, provider_id, status,
                   payload_json, updated_at
            FROM operations
            WHERE run_id = ? AND task_id = ? AND operation = ?
            """,
            (run_id, task_id, operation),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        return result

    async def count_operations(self) -> int:
        cursor = await self._connection.execute(
            "SELECT COUNT(*) FROM operations"
        )
        row = await cursor.fetchone()
        await cursor.close()
        return int(row[0]) if row is not None else 0

    async def save_artifact(
        self,
        run_id: str,
        artifact: Artifact,
    ) -> None:
        artifact_json = artifact.model_dump_json()
        await self._connection.execute(
            """
            INSERT INTO artifacts (
              artifact_id, run_id, task_id, artifact_json, sha256, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(artifact_id) DO UPDATE SET
              run_id = excluded.run_id,
              task_id = excluded.task_id,
              artifact_json = excluded.artifact_json,
              sha256 = excluded.sha256,
              updated_at = excluded.updated_at
            """,
            (
                artifact.artifact_id,
                run_id,
                artifact.task_id,
                artifact_json,
                artifact.sha256,
                _now(),
            ),
        )
        await self._connection.commit()
