import asyncio
import json
import re
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

import aiosqlite

from feishu_generation_agent.domain import Artifact, VisionDescription


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
  phase TEXT NOT NULL DEFAULT 'failed',
  client_submission_id TEXT,
  official_id TEXT,
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
_CLIENT_SUBMISSION_ID = re.compile(r"[0-9a-f]{32}\Z")
_SAFE_PROVIDER = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_OPERATION_PHASES = frozenset(
    {
        "intent_created",
        "submitted",
        "submission_uncertain",
        "succeeded",
        "failed",
        "cancelled",
        "expired",
        "timed_out",
    }
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _safe_summary(summary: str) -> str:
    redacted = _BEARER_TOKEN.sub(r"\1[REDACTED]", summary)
    redacted = _QUERY_TOKEN.sub(r"\1[REDACTED]", redacted)
    return redacted[:500]


class Repository:
    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection
        self._write_lock = asyncio.Lock()

    @classmethod
    async def open(cls, path: Path) -> "Repository":
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = await aiosqlite.connect(path)
        connection.row_factory = aiosqlite.Row
        try:
            await connection.executescript(_SCHEMA)
            await cls._migrate_operations(connection)
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
        await self._write(
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

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        cursor = await self._connection.execute(
            """
            SELECT run_id, thread_id, source_url, status, created_at, updated_at
            FROM runs
            WHERE run_id = ?
            """,
            (run_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return dict(row) if row is not None else None

    async def update_run_status(self, run_id: str, status: str) -> bool:
        async with self._write_lock:
            try:
                cursor = await self._connection.execute(
                    """
                    UPDATE runs
                    SET status = ?, updated_at = ?
                    WHERE run_id = ?
                    """,
                    (status, _now(), run_id),
                )
                await self._connection.commit()
            except BaseException:
                await self._connection.rollback()
                raise
        changed = cursor.rowcount > 0
        await cursor.close()
        return changed

    async def append_event(
        self,
        run_id: str,
        node: str,
        status: str,
        summary: str,
    ) -> None:
        await self._write(
            """
            INSERT INTO events (run_id, node, status, summary, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (run_id, node, status, _safe_summary(summary), _now()),
        )

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
        phase = self._legacy_phase(status)
        await self._write(
            """
            INSERT INTO operations (
              run_id, task_id, operation, provider_id, status,
              phase, client_submission_id, official_id,
              payload_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
            ON CONFLICT(run_id, task_id, operation) DO UPDATE SET
              provider_id = excluded.provider_id,
              status = excluded.status,
              phase = excluded.phase,
              official_id = excluded.official_id,
              payload_json = excluded.payload_json,
              updated_at = excluded.updated_at
            """,
            (
                run_id,
                task_id,
                operation,
                provider_id,
                status,
                phase,
                provider_id,
                payload_json,
                _now(),
            ),
        )

    async def create_submission_intent_if_absent(
        self,
        run_id: str,
        task_id: str,
        provider: str,
        client_submission_id: str,
    ) -> tuple[bool, dict[str, Any]]:
        self._validate_identity_segment(run_id, "run_id")
        self._validate_identity_segment(task_id, "task_id")
        if not isinstance(provider, str) or _SAFE_PROVIDER.fullmatch(provider) is None:
            raise ValueError("invalid provider")
        self._validate_client_submission_id(client_submission_id)
        payload_json = json.dumps(
            {"provider": provider},
            ensure_ascii=True,
            separators=(",", ":"),
        )
        timestamp = _now()
        async with self._write_lock:
            try:
                await self._connection.execute("BEGIN IMMEDIATE")
                cursor = await self._connection.execute(
                    """
                    INSERT OR IGNORE INTO operations (
                      run_id, task_id, operation, provider_id, status,
                      phase, client_submission_id, official_id,
                      payload_json, updated_at
                    ) VALUES (?, ?, 'submit', NULL, 'intent_created',
                              'intent_created', ?, NULL, ?, ?)
                    """,
                    (
                        run_id,
                        task_id,
                        client_submission_id,
                        payload_json,
                        timestamp,
                    ),
                )
                created = cursor.rowcount == 1
                await cursor.close()
                select_cursor = await self._connection.execute(
                    """
                    SELECT run_id, task_id, operation, provider_id, status,
                           phase, client_submission_id, official_id,
                           payload_json, updated_at
                    FROM operations
                    WHERE run_id = ? AND task_id = ? AND operation = 'submit'
                    """,
                    (run_id, task_id),
                )
                row = await select_cursor.fetchone()
                await select_cursor.close()
                if row is None:
                    raise RuntimeError("submission intent was not persisted")
                await self._connection.commit()
            except BaseException:
                await self._connection.rollback()
                raise
        return created, self._operation_from_row(row)

    async def compare_and_set_operation(
        self,
        run_id: str,
        task_id: str,
        operation: str,
        *,
        expected_phase: str,
        expected_client_submission_id: str,
        expected_official_id: str | None,
        phase: str,
        official_id: str | None,
    ) -> bool:
        self._validate_identity_segment(run_id, "run_id")
        self._validate_identity_segment(task_id, "task_id")
        self._validate_identity_segment(operation, "operation")
        self._validate_phase(expected_phase)
        self._validate_phase(phase)
        self._validate_client_submission_id(expected_client_submission_id)
        if expected_official_id is not None:
            self._validate_official_id(expected_official_id)
        if phase == "submitted" and official_id is None:
            raise ValueError("submitted operation requires official_id")
        if official_id is not None:
            self._validate_official_id(official_id)
        async with self._write_lock:
            try:
                cursor = await self._connection.execute(
                    """
                    UPDATE operations
                    SET provider_id = ?, status = ?, phase = ?,
                        official_id = ?, updated_at = ?
                    WHERE run_id = ? AND task_id = ? AND operation = ?
                      AND phase = ?
                      AND client_submission_id = ?
                      AND official_id IS ?
                    """,
                    (
                        official_id,
                        phase,
                        phase,
                        official_id,
                        _now(),
                        run_id,
                        task_id,
                        operation,
                        expected_phase,
                        expected_client_submission_id,
                        expected_official_id,
                    ),
                )
                changed = cursor.rowcount == 1
                await self._connection.commit()
            except BaseException:
                await self._connection.rollback()
                raise
        await cursor.close()
        return changed

    async def get_operation(
        self,
        run_id: str,
        task_id: str,
        operation: str,
    ) -> dict[str, Any] | None:
        cursor = await self._connection.execute(
            """
            SELECT run_id, task_id, operation, provider_id, status,
                   phase, client_submission_id, official_id,
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
        return self._operation_from_row(row)

    async def list_operations(
        self,
        run_id: str,
        *,
        task_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if task_id is None:
            sql = """
                SELECT run_id, task_id, operation, provider_id, status,
                       phase, client_submission_id, official_id,
                       payload_json, updated_at
                FROM operations
                WHERE run_id = ?
                ORDER BY task_id, operation
            """
            parameters = (run_id,)
        else:
            sql = """
                SELECT run_id, task_id, operation, provider_id, status,
                       phase, client_submission_id, official_id,
                       payload_json, updated_at
                FROM operations
                WHERE run_id = ? AND task_id = ?
                ORDER BY operation
            """
            parameters = (run_id, task_id)
        cursor = await self._connection.execute(sql, parameters)
        rows = await cursor.fetchall()
        await cursor.close()
        return [self._operation_from_row(row) for row in rows]

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
        await self._write(
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

    async def get_artifact(self, artifact_id: str) -> Artifact | None:
        cursor = await self._connection.execute(
            """
            SELECT artifact_json
            FROM artifacts
            WHERE artifact_id = ?
            """,
            (artifact_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return Artifact.model_validate_json(row[0]) if row is not None else None

    async def list_artifacts(
        self,
        run_id: str,
        *,
        task_id: str | None = None,
    ) -> list[Artifact]:
        if task_id is None:
            sql = """
                SELECT artifact_json
                FROM artifacts
                WHERE run_id = ?
                ORDER BY artifact_id
            """
            parameters = (run_id,)
        else:
            sql = """
                SELECT artifact_json
                FROM artifacts
                WHERE run_id = ? AND task_id = ?
                ORDER BY artifact_id
            """
            parameters = (run_id, task_id)
        cursor = await self._connection.execute(sql, parameters)
        rows = await cursor.fetchall()
        await cursor.close()
        return [Artifact.model_validate_json(row[0]) for row in rows]

    async def get_vision_cache(
        self,
        cache_key: str,
    ) -> dict[str, Any] | None:
        cursor = await self._connection.execute(
            """
            SELECT description_json
            FROM vision_cache
            WHERE cache_key = ?
            """,
            (cache_key,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        description = json.loads(row[0])
        if not isinstance(description, dict):
            raise ValueError("vision cache entry is not a JSON object")
        return description

    async def save_vision_cache(
        self,
        cache_key: str,
        description: VisionDescription,
    ) -> None:
        description_json = json.dumps(
            description.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        await self._write(
            """
            INSERT INTO vision_cache (cache_key, description_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
              description_json = excluded.description_json,
              updated_at = excluded.updated_at
            """,
            (cache_key, description_json, _now()),
        )

    async def _write(self, sql: str, parameters: tuple[Any, ...]) -> None:
        async with self._write_lock:
            try:
                await self._connection.execute(sql, parameters)
                await self._connection.commit()
            except BaseException:
                await self._connection.rollback()
                raise

    @staticmethod
    async def _migrate_operations(connection: aiosqlite.Connection) -> None:
        cursor = await connection.execute("PRAGMA table_info(operations)")
        rows = await cursor.fetchall()
        await cursor.close()
        columns = {str(row[1]) for row in rows}
        additions = {
            "phase": "TEXT",
            "client_submission_id": "TEXT",
            "official_id": "TEXT",
        }
        for name, declaration in additions.items():
            if name not in columns:
                await connection.execute(
                    f"ALTER TABLE operations ADD COLUMN {name} {declaration}"
                )
        await connection.execute(
            """
            UPDATE operations
            SET phase = CASE status
              WHEN 'intent_created' THEN 'intent_created'
              WHEN 'submitted' THEN 'submitted'
              WHEN 'submission_uncertain' THEN 'submission_uncertain'
              WHEN 'succeeded' THEN 'succeeded'
              WHEN 'completed' THEN 'succeeded'
              WHEN 'cancelled' THEN 'cancelled'
              WHEN 'expired' THEN 'expired'
              WHEN 'timed_out' THEN 'timed_out'
              ELSE 'failed'
            END
            WHERE phase IS NULL OR phase = ''
            """
        )
        cursor = await connection.execute(
            """
            SELECT rowid, run_id, task_id, operation
            FROM operations
            WHERE phase = 'submitted'
              AND (client_submission_id IS NULL OR client_submission_id = '')
            """
        )
        legacy_submitted = await cursor.fetchall()
        await cursor.close()
        for row in legacy_submitted:
            client_submission_id = sha256(
                (
                    f"legacy\0{row[1]}\0{row[2]}\0{row[3]}"
                ).encode("utf-8")
            ).hexdigest()[:32]
            await connection.execute(
                """
                UPDATE operations
                SET client_submission_id = ?
                WHERE rowid = ? AND phase = 'submitted'
                  AND (client_submission_id IS NULL OR client_submission_id = '')
                """,
                (client_submission_id, row[0]),
            )
        await connection.execute(
            """
            UPDATE operations
            SET official_id = provider_id
            WHERE official_id IS NULL AND provider_id IS NOT NULL
            """
        )

    @staticmethod
    def _operation_from_row(row: aiosqlite.Row) -> dict[str, Any]:
        result = dict(row)
        payload = json.loads(result.pop("payload_json"))
        if not isinstance(payload, dict):
            raise ValueError("operation payload must be a JSON object")
        result["payload"] = payload
        return result

    @staticmethod
    def _legacy_phase(status: str) -> str:
        return {
            "completed": "succeeded",
        }.get(status, status if status in _OPERATION_PHASES else "failed")

    @staticmethod
    def _validate_identity_segment(value: str, field_name: str) -> None:
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 255
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ValueError(f"invalid {field_name}")

    @staticmethod
    def _validate_client_submission_id(value: str) -> None:
        if not isinstance(value, str) or _CLIENT_SUBMISSION_ID.fullmatch(value) is None:
            raise ValueError("invalid client_submission_id")

    @staticmethod
    def _validate_official_id(value: str) -> None:
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or len(value) > 512
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ValueError("invalid official_id")

    @staticmethod
    def _validate_phase(value: str) -> None:
        if value not in _OPERATION_PHASES:
            raise ValueError("invalid operation phase")
