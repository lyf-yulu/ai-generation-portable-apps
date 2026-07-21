import asyncio
import json
import re
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qsl, urlsplit

import aiosqlite

from feishu_generation_agent.domain.bitable import BitableBinding, TableTaskStatus


_SCHEMA = """
CREATE TABLE IF NOT EXISTS bitable_tasks (
  app_token TEXT NOT NULL,
  table_id TEXT NOT NULL,
  record_id TEXT NOT NULL,
  view_id TEXT NOT NULL,
  source_url TEXT NOT NULL,
  display_text TEXT NOT NULL,
  run_id TEXT NOT NULL UNIQUE,
  thread_id TEXT NOT NULL UNIQUE,
  claimant_open_id TEXT NOT NULL,
  status TEXT NOT NULL,
  approval_version INTEGER NOT NULL DEFAULT 0,
  plan_fingerprint TEXT,
  reply_context_json TEXT NOT NULL DEFAULT '{}',
  last_error TEXT,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (app_token, table_id, record_id)
);
CREATE TABLE IF NOT EXISTS bot_ingress (
  dedupe_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  command_json TEXT NOT NULL,
  status TEXT NOT NULL,
  result_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS card_actions (
  action_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  command_json TEXT NOT NULL,
  status TEXT NOT NULL,
  result_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
"""

_RELEASE_STATUSES = {
    TableTaskStatus.PENDING,
    TableTaskStatus.COMPLETED,
    TableTaskStatus.FAILED,
    TableTaskStatus.WRITEBACK_FAILED,
}
_MAX_COMMAND_JSON_BYTES = 64 * 1024
_SENSITIVE_COMMAND_KEYS = {
    "api_key",
    "app_secret",
    "auth",
    "authorization",
    "bearer_token",
    "client_secret",
    "client_token",
    "cookie",
    "cookies",
    "credential",
    "credentials",
    "event",
    "headers",
    "id_token",
    "private_key",
    "payload",
    "raw_event",
    "raw_payload",
    "refresh_token",
    "secret",
    "signature",
    "signed_url",
    "tenant_access_token",
    "token",
}
_SENSITIVE_COMMAND_KEY_SUFFIXES = (
    "_access_token",
    "_api_key",
    "_authorization",
    "_credential",
    "_headers",
    "_private_key",
    "_secret",
    "_signature",
)
_SENSITIVE_QUERY_MARKERS = (
    "access_key",
    "api_key",
    "credential",
    "secret",
    "signature",
    "token",
)
_URL_CANDIDATE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


class TaskAlreadyClaimed(RuntimeError):
    def __init__(self, binding: BitableBinding) -> None:
        super().__init__(
            "Bitable record is already claimed: "
            f"{binding.app_token}/{binding.table_id}/{binding.record_id}"
        )
        self.binding = binding


class BitableTaskStore:
    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection
        self._lock = asyncio.Lock()

    @classmethod
    async def open(cls, path: str | Path) -> "BitableTaskStore":
        database_path = Path(path)
        database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = await aiosqlite.connect(
            str(database_path),
            isolation_level=None,
        )
        connection.row_factory = aiosqlite.Row
        await connection.execute("PRAGMA busy_timeout = 5000")
        await connection.executescript(_SCHEMA)
        return cls(connection)

    async def close(self) -> None:
        await self._connection.close()

    async def claim(
        self,
        *,
        app_token: str,
        table_id: str,
        view_id: str,
        record_id: str,
        source_url: str,
        display_text: str,
        claimant_open_id: str,
        run_id: str,
        thread_id: str,
        reply_context: dict[str, str],
    ) -> BitableBinding:
        reply_context_json = _json_object(reply_context, field_name="reply_context")
        async with self._lock:
            try:
                await self._connection.execute("BEGIN IMMEDIATE")
                cursor = await self._connection.execute(
                    """
                    SELECT * FROM bitable_tasks
                    WHERE app_token = ? AND table_id = ? AND record_id = ?
                    """,
                    (app_token, table_id, record_id),
                )
                existing = await cursor.fetchone()
                await cursor.close()
                if existing is not None and existing["active"] == 1:
                    raise TaskAlreadyClaimed(_binding_from_row(existing))

                parameters = (
                    view_id,
                    source_url,
                    display_text,
                    run_id,
                    thread_id,
                    claimant_open_id,
                    TableTaskStatus.PROCESSING.value,
                    reply_context_json,
                )
                if existing is None:
                    await self._connection.execute(
                        """
                        INSERT INTO bitable_tasks (
                          app_token, table_id, record_id, view_id, source_url,
                          display_text, run_id, thread_id, claimant_open_id,
                          status, reply_context_json, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                  CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                        """,
                        (app_token, table_id, record_id, *parameters),
                    )
                else:
                    await self._connection.execute(
                        """
                        UPDATE bitable_tasks
                        SET view_id = ?, source_url = ?, display_text = ?,
                            run_id = ?, thread_id = ?, claimant_open_id = ?,
                            status = ?, approval_version = 0,
                            plan_fingerprint = NULL, reply_context_json = ?,
                            last_error = NULL, active = 1,
                            created_at = CURRENT_TIMESTAMP,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE app_token = ? AND table_id = ? AND record_id = ?
                        """,
                        (*parameters, app_token, table_id, record_id),
                    )

                cursor = await self._connection.execute(
                    """
                    SELECT * FROM bitable_tasks
                    WHERE app_token = ? AND table_id = ? AND record_id = ?
                    """,
                    (app_token, table_id, record_id),
                )
                row = await cursor.fetchone()
                await cursor.close()
                await self._connection.commit()
            except BaseException:
                await asyncio.shield(self._connection.rollback())
                raise

        assert row is not None
        return _binding_from_row(row)

    async def get_by_record(
        self,
        app_token: str,
        table_id: str,
        record_id: str,
    ) -> BitableBinding | None:
        async with self._lock:
            cursor = await self._connection.execute(
                """
                SELECT * FROM bitable_tasks
                WHERE app_token = ? AND table_id = ? AND record_id = ?
                """,
                (app_token, table_id, record_id),
            )
            row = await cursor.fetchone()
            await cursor.close()
        return None if row is None else _binding_from_row(row)

    async def get_by_run(self, run_id: str) -> BitableBinding | None:
        async with self._lock:
            cursor = await self._connection.execute(
                "SELECT * FROM bitable_tasks WHERE run_id = ?",
                (run_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
        return None if row is None else _binding_from_row(row)

    async def set_status(
        self,
        run_id: str,
        status: TableTaskStatus,
        last_error: str | None = None,
    ) -> BitableBinding:
        return await self._update_run(
            run_id,
            """
            UPDATE bitable_tasks
            SET status = ?, last_error = ?, updated_at = CURRENT_TIMESTAMP
            WHERE run_id = ?
            """,
            (status.value, last_error, run_id),
        )

    async def release(
        self,
        run_id: str,
        *,
        status: TableTaskStatus,
        last_error: str | None = None,
    ) -> BitableBinding:
        if status not in _RELEASE_STATUSES:
            raise ValueError(f"invalid release status: {status}")
        return await self._update_run(
            run_id,
            """
            UPDATE bitable_tasks
            SET status = ?, last_error = ?, active = 0,
                updated_at = CURRENT_TIMESTAMP
            WHERE run_id = ?
            """,
            (status.value, last_error, run_id),
        )

    async def advance_approval(
        self,
        run_id: str,
        plan_fingerprint: str,
    ) -> BitableBinding:
        async with self._lock:
            try:
                await self._connection.execute("BEGIN IMMEDIATE")
                row = await self._fetch_run(run_id)
                if row is None:
                    raise KeyError(f"unknown run_id: {run_id}")
                if row["plan_fingerprint"] != plan_fingerprint:
                    await self._connection.execute(
                        """
                        UPDATE bitable_tasks
                        SET approval_version = approval_version + 1,
                            plan_fingerprint = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE run_id = ?
                        """,
                        (plan_fingerprint, run_id),
                    )
                    row = await self._fetch_run(run_id)
                await self._connection.commit()
            except BaseException:
                await asyncio.shield(self._connection.rollback())
                raise

        assert row is not None
        return _binding_from_row(row)

    async def accept_ingress(
        self,
        *,
        dedupe_id: str,
        kind: str,
        command: dict[str, Any],
    ) -> bool:
        return await self._accept_once(
            table="bot_ingress",
            id_column="dedupe_id",
            item_id=dedupe_id,
            kind=kind,
            command=command,
        )

    async def accept_action(
        self,
        *,
        action_id: str,
        kind: str,
        command: dict[str, Any],
    ) -> bool:
        return await self._accept_once(
            table="card_actions",
            id_column="action_id",
            item_id=action_id,
            kind=kind,
            command=command,
        )

    async def finish_ingress(
        self,
        dedupe_id: str,
        *,
        status: Literal["completed", "failed"],
        result: dict[str, Any] | None = None,
    ) -> bool:
        return await self._finish_once(
            table="bot_ingress",
            id_column="dedupe_id",
            item_id=dedupe_id,
            status=status,
            result=result,
        )

    async def finish_action(
        self,
        action_id: str,
        *,
        status: Literal["completed", "failed"],
        result: dict[str, Any] | None = None,
    ) -> bool:
        return await self._finish_once(
            table="card_actions",
            id_column="action_id",
            item_id=action_id,
            status=status,
            result=result,
        )

    async def _accept_once(
        self,
        *,
        table: str,
        id_column: str,
        item_id: str,
        kind: str,
        command: dict[str, Any],
    ) -> bool:
        command_json = _command_json(command)
        async with self._lock:
            cursor = await self._connection.execute(
                f"""
                INSERT OR IGNORE INTO {table} (
                  {id_column}, kind, command_json, status, created_at, updated_at
                ) VALUES (?, ?, ?, 'pending', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (item_id, kind, command_json),
            )
            inserted = cursor.rowcount == 1
            await cursor.close()
        return inserted

    async def _finish_once(
        self,
        *,
        table: str,
        id_column: str,
        item_id: str,
        status: Literal["completed", "failed"],
        result: dict[str, Any] | None,
    ) -> bool:
        if status not in {"completed", "failed"}:
            raise ValueError(f"invalid finish status: {status}")
        if result is None:
            async with self._lock:
                cursor = await self._connection.execute(
                    f"SELECT status FROM {table} WHERE {id_column} = ?",
                    (item_id,),
                )
                row = await cursor.fetchone()
                await cursor.close()
            if row is None or row["status"] != "pending":
                return False
            raise TypeError("result must be a JSON object")
        result_json = _json_object(result, field_name="result")
        async with self._lock:
            cursor = await self._connection.execute(
                f"""
                UPDATE {table}
                SET status = ?, result_json = ?, updated_at = CURRENT_TIMESTAMP
                WHERE {id_column} = ? AND status = 'pending'
                """,
                (status, result_json, item_id),
            )
            finished = cursor.rowcount == 1
            await cursor.close()
        return finished

    async def _update_run(
        self,
        run_id: str,
        statement: str,
        parameters: tuple[Any, ...],
    ) -> BitableBinding:
        async with self._lock:
            try:
                await self._connection.execute("BEGIN IMMEDIATE")
                cursor = await self._connection.execute(statement, parameters)
                updated = cursor.rowcount == 1
                await cursor.close()
                if not updated:
                    raise KeyError(f"unknown run_id: {run_id}")
                row = await self._fetch_run(run_id)
                await self._connection.commit()
            except BaseException:
                await asyncio.shield(self._connection.rollback())
                raise

        assert row is not None
        return _binding_from_row(row)

    async def _fetch_run(self, run_id: str) -> aiosqlite.Row | None:
        cursor = await self._connection.execute(
            "SELECT * FROM bitable_tasks WHERE run_id = ?",
            (run_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row


def _json_object(value: dict[str, Any], *, field_name: str) -> str:
    if not isinstance(value, dict):
        raise TypeError(f"{field_name} must be a JSON object")
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be JSON serializable") from exc


def _command_json(command: dict[str, Any]) -> str:
    if not isinstance(command, dict):
        raise TypeError("command must be a JSON object")
    _reject_sensitive_command_content(command, path="command")
    serialized = _json_object(command, field_name="command")
    if len(serialized.encode("utf-8")) > _MAX_COMMAND_JSON_BYTES:
        raise ValueError("command JSON is too large")
    return serialized


def _reject_sensitive_command_content(value: Any, *, path: str) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise ValueError(f"command JSON object key at {path} must be a string")
            normalized_key = _normalize_key(key)
            if normalized_key in _SENSITIVE_COMMAND_KEYS or normalized_key.endswith(
                _SENSITIVE_COMMAND_KEY_SUFFIXES
            ):
                raise ValueError(f"command contains sensitive field at {path}.{key}")
            _reject_sensitive_command_content(nested, path=f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            _reject_sensitive_command_content(nested, path=f"{path}[{index}]")
        return
    if isinstance(value, str) and _contains_sensitive_url_query(value):
        raise ValueError(f"command contains sensitive URL at {path}")


def _normalize_key(value: str) -> str:
    with_acronym_boundaries = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "_", value)
    with_word_boundaries = re.sub(
        r"(?<=[a-z0-9])(?=[A-Z])", "_", with_acronym_boundaries
    )
    return re.sub(r"[^a-z0-9]+", "_", with_word_boundaries.casefold()).strip("_")


def _contains_sensitive_url_query(value: str) -> bool:
    return any(
        _has_sensitive_url_query(match.group(0).rstrip(".,;:!?)]}"))
        for match in _URL_CANDIDATE.finditer(value)
    )


def _has_sensitive_url_query(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
            return False
        query_keys = [_normalize_key(key) for key, _ in parse_qsl(parsed.query)]
    except (TypeError, ValueError):
        return False
    return any(
        key in {"key", "sig"}
        or any(
            marker in key
            or marker.replace("_", "") in key.replace("_", "")
            for marker in _SENSITIVE_QUERY_MARKERS
        )
        for key in query_keys
    )


def _binding_from_row(row: aiosqlite.Row) -> BitableBinding:
    return BitableBinding(
        app_token=row["app_token"],
        table_id=row["table_id"],
        view_id=row["view_id"],
        record_id=row["record_id"],
        source_url=row["source_url"],
        display_text=row["display_text"],
        run_id=row["run_id"],
        thread_id=row["thread_id"],
        claimant_open_id=row["claimant_open_id"],
        status=TableTaskStatus(row["status"]),
        approval_version=row["approval_version"],
        plan_fingerprint=row["plan_fingerprint"],
        reply_context=json.loads(row["reply_context_json"]),
        last_error=row["last_error"],
    )
