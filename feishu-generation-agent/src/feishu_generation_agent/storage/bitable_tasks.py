import asyncio
import base64
import binascii
import json
import re
from collections import Counter
from math import log2
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qsl, urlsplit

import aiosqlite

from feishu_generation_agent.domain.bitable import BitableBinding, TableTaskStatus
from feishu_generation_agent.integrations.bitable_url import parse_requirement_source


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
_MAX_JSON_BYTES = 64 * 1024
_MAX_JSON_DEPTH = 12
_MAX_JSON_NODES = 1024
_MAX_TEXT_BYTES = 16 * 1024
_MAX_SOURCE_URL_BYTES = 2 * 1024
_MAX_DISPLAY_TEXT_BYTES = 4 * 1024
_MAX_IDENTIFIER_BYTES = 256
_MAX_KIND_BYTES = 64
_URL_CANDIDATE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_CONTROL_CHARACTER = re.compile(r"[\x00-\x1f\x7f]")
_DATA_URL = re.compile(r"data:[^,\s]*;base64,", re.IGNORECASE)
_BASE64_CANDIDATE = re.compile(
    r"(?<![A-Za-z0-9+/=_-])[A-Za-z0-9+/_-]{4,}={0,2}"
    r"(?![A-Za-z0-9+/=_-])"
)
_WRAPPED_BASE64_CANDIDATE = re.compile(
    r"(?<![A-Za-z0-9+/=_-])"
    r"(?:(?:[A-Za-z0-9+/_-]{4})+[ \t\r\n]+)+"
    r"[A-Za-z0-9+/_-]{2,}={0,2}"
    r"(?![A-Za-z0-9+/=_-])"
)
_BEARER_CREDENTIAL = re.compile(r"\bbearer\s+[^\s,;]+", re.IGNORECASE)
_LABELED_CREDENTIAL = re.compile(
    r"\b(?:(?:[a-z0-9]+[\s_-]+)*token|access[\s_-]*key(?:[\s_-]*id)?|"
    r"api[\s_-]*key|authorization|jwt|password|secret)"
    r"\s*[:=]\s*[^\s,;]+",
    re.IGNORECASE,
)
_QUOTED_LABEL = re.compile(
    r'''["']\s*([A-Za-z][A-Za-z0-9 _-]{0,79})\s*["']\s*:'''
)
_SENSITIVE_SINGLE_KEY_SEGMENTS = {
    "auth",
    "authorization",
    "cookie",
    "cookies",
    "credential",
    "credentials",
    "header",
    "headers",
    "jwt",
    "password",
    "passwd",
    "secret",
    "signature",
}
_SENSITIVE_KEY_PAIRS = {
    ("access", "token"),
    ("access", "key"),
    ("api", "key"),
    ("app", "secret"),
    ("bearer", "token"),
    ("client", "secret"),
    ("client", "token"),
    ("id", "token"),
    ("private", "key"),
    ("refresh", "token"),
    ("session", "token"),
    ("tenant", "token"),
    ("user", "token"),
}
_SAFE_RESOURCE_TOKEN_KEYS = {"app_token", "file_token", "wiki_token"}
_SENSITIVE_COMPACT_KEY_MARKERS = {
    "accesskey",
    "accesskeyid",
    "accesstoken",
    "apikey",
    "appsecret",
    "authorization",
    "bearer",
    "clientsecret",
    "credential",
    "oauthtoken",
    "password",
    "privatekey",
    "secretkey",
    "signature",
    "verificationtoken",
    "xapikey",
}
_STRUCTURAL_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*\Z")
_PLAN_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
_SOURCE_PATH = re.compile(r"/(?:docx|wiki)/[A-Za-z0-9_-]+\Z")
_JSON_UNICODE_ESCAPE = re.compile(r"(?<!\\)\\u([0-9a-fA-F]{4})")
_JSON_ESCAPE = re.compile(r'''\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4})''')
_JSON_SOLIDUS_ESCAPE = re.compile(r"(?<!\\)\\/")


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
        app_token = _safe_identifier(app_token, field_name="app_token")
        table_id = _safe_identifier(table_id, field_name="table_id")
        view_id = _safe_identifier(view_id, field_name="view_id")
        record_id = _safe_identifier(record_id, field_name="record_id")
        run_id = _safe_identifier(run_id, field_name="run_id")
        thread_id = _safe_identifier(thread_id, field_name="thread_id")
        claimant_open_id = _safe_identifier(
            claimant_open_id,
            field_name="claimant_open_id",
        )
        source_url = _safe_requirement_source(source_url)
        display_text = _safe_required_text(
            display_text,
            field_name="display_text",
            max_bytes=_MAX_DISPLAY_TEXT_BYTES,
        )
        reply_context_json = _reply_context_json(reply_context)
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
                await _commit_durably(self._connection)
            except BaseException:
                await _rollback_durably(self._connection)
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
        run_id = _safe_identifier(run_id, field_name="run_id")
        status = _task_status(status)
        last_error = _safe_optional_text(last_error, field_name="last_error")
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
        run_id = _safe_identifier(run_id, field_name="run_id")
        status = _task_status(status)
        if status not in _RELEASE_STATUSES:
            raise ValueError(f"invalid release status: {status}")
        last_error = _safe_optional_text(last_error, field_name="last_error")
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
    ) -> tuple[BitableBinding, bool]:
        run_id = _safe_identifier(run_id, field_name="run_id")
        plan_fingerprint = _safe_plan_fingerprint(plan_fingerprint)
        async with self._lock:
            try:
                await self._connection.execute("BEGIN IMMEDIATE")
                row = await self._fetch_run(run_id)
                if row is None:
                    raise KeyError(f"unknown run_id: {run_id}")
                changed = row["plan_fingerprint"] != plan_fingerprint
                if changed:
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
                await _commit_durably(self._connection)
            except BaseException:
                await _rollback_durably(self._connection)
                raise

        assert row is not None
        return _binding_from_row(row), changed

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
        item_id = _safe_identifier(item_id, field_name=id_column)
        kind = _safe_identifier(
            kind,
            field_name="kind",
            max_bytes=_MAX_KIND_BYTES,
        )
        command_json = _json_object(command, field_name="command")
        async with self._lock:
            try:
                await self._connection.execute("BEGIN IMMEDIATE")
                cursor = await self._connection.execute(
                    f"""
                    INSERT INTO {table} (
                      {id_column}, kind, command_json, status, created_at, updated_at
                    ) VALUES (?, ?, ?, 'pending', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    ON CONFLICT({id_column}) DO NOTHING
                    """,
                    (item_id, kind, command_json),
                )
                inserted = cursor.rowcount == 1
                await cursor.close()
                await _commit_durably(self._connection)
            except BaseException:
                await _rollback_durably(self._connection)
                raise
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
        item_id = _safe_identifier(item_id, field_name=id_column)
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
            try:
                await self._connection.execute("BEGIN IMMEDIATE")
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
                await _commit_durably(self._connection)
            except BaseException:
                await _rollback_durably(self._connection)
                raise
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
                await _commit_durably(self._connection)
            except BaseException:
                await _rollback_durably(self._connection)
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
    _validate_json_content(value, field_name=field_name)
    try:
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be JSON serializable") from exc
    if len(serialized.encode("utf-8")) > _MAX_JSON_BYTES:
        raise ValueError(f"{field_name} JSON is too large")
    return serialized


def _reply_context_json(value: dict[str, str]) -> str:
    if not isinstance(value, dict) or any(
        not isinstance(key, str) or not isinstance(item, str)
        for key, item in value.items()
    ):
        raise TypeError("reply_context must contain only string keys and values")
    return _json_object(value, field_name="reply_context")


def _safe_identifier(
    value: str,
    *,
    field_name: str,
    max_bytes: int = _MAX_IDENTIFIER_BYTES,
) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be text")
    if (
        not value
        or _bounded_utf8_size(value, max_bytes=max_bytes) is None
        or _STRUCTURAL_IDENTIFIER.fullmatch(value) is None
    ):
        raise ValueError(f"{field_name} has an invalid format")
    return value


def _task_status(value: TableTaskStatus) -> TableTaskStatus:
    if not isinstance(value, TableTaskStatus):
        raise ValueError("status must be a valid TableTaskStatus")
    return value


def _safe_plan_fingerprint(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("plan_fingerprint must be text")
    if _PLAN_FINGERPRINT.fullmatch(value) is None:
        raise ValueError("plan_fingerprint must be a lowercase SHA-256 digest")
    return value


def _safe_required_text(value: str, *, field_name: str, max_bytes: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be text")
    if not value or _bounded_utf8_size(value, max_bytes=max_bytes) is None:
        raise ValueError(f"{field_name} is empty or too large")
    _validate_safe_text(value, field_name=field_name)
    return value


def _safe_requirement_source(value: str) -> str:
    value = _safe_required_text(
        value,
        field_name="source_url",
        max_bytes=_MAX_SOURCE_URL_BYTES,
    )
    try:
        source = urlsplit(value)
        if (
            source.username is not None
            or source.password is not None
            or source.port not in {None, 443}
        ):
            raise ValueError
        normalized = parse_requirement_source(value)
        if _CONTROL_CHARACTER.search(normalized):
            raise ValueError
        _validate_safe_text(normalized, field_name="source_url")
        parsed = urlsplit(normalized)
        if (
            parsed.port is not None
            or parsed.query
            or parsed.fragment
            or _SOURCE_PATH.fullmatch(parsed.path) is None
        ):
            raise ValueError
        return normalized
    except (TypeError, ValueError):
        raise ValueError("source_url must be a safe Feishu docx/wiki URL") from None


def _validate_json_content(value: dict[str, Any], *, field_name: str) -> None:
    stack: list[tuple[Any, int]] = [(value, 0)]
    nodes = 0
    byte_budget = 0

    def consume_bytes(amount: int) -> None:
        nonlocal byte_budget
        byte_budget += amount
        if byte_budget > _MAX_JSON_BYTES:
            raise ValueError(f"{field_name} JSON is too large")

    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > _MAX_JSON_NODES:
            raise ValueError(f"{field_name} JSON has too many nodes")
        if depth > _MAX_JSON_DEPTH:
            raise ValueError(f"{field_name} JSON is too deep")
        if isinstance(current, dict):
            consume_bytes(2 + max(0, len(current) - 1))
            for key, nested in current.items():
                if not isinstance(key, str):
                    raise ValueError(f"{field_name} JSON object keys must be strings")
                key_size = _bounded_utf8_size(key, max_bytes=_MAX_JSON_BYTES)
                if key_size is None:
                    raise ValueError(f"{field_name} JSON is too large")
                consume_bytes(key_size + 3)
                if _is_sensitive_key(key):
                    raise ValueError(f"{field_name} contains sensitive content")
                stack.append((nested, depth + 1))
        elif isinstance(current, (list, tuple)):
            consume_bytes(2 + max(0, len(current) - 1))
            stack.extend((nested, depth + 1) for nested in current)
        elif isinstance(current, str):
            text_size = _bounded_utf8_size(current, max_bytes=_MAX_JSON_BYTES)
            if text_size is None:
                raise ValueError(f"{field_name} JSON is too large")
            consume_bytes(text_size + 2)
            _validate_safe_text(current, field_name=field_name)


def _safe_optional_text(value: str | None, *, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be text or None")
    if _bounded_utf8_size(value, max_bytes=_MAX_TEXT_BYTES) is None:
        raise ValueError(f"{field_name} is too large")
    _validate_safe_text(value, field_name=field_name)
    return value


def _bounded_utf8_size(value: str, *, max_bytes: int) -> int | None:
    if len(value) > max_bytes:
        return None
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        return None
    return size if size <= max_bytes else None


def _validate_safe_text(value: str, *, field_name: str) -> None:
    decoded = value
    for _ in range(_MAX_JSON_DEPTH):
        if _contains_sensitive_text(decoded):
            raise ValueError(f"{field_name} contains sensitive content")
        unescaped = _decode_json_text_layer(decoded)
        if _bounded_utf8_size(unescaped, max_bytes=_MAX_JSON_BYTES) is None:
            raise ValueError(f"{field_name} is too large")
        if unescaped == decoded:
            return
        decoded = unescaped
    if _JSON_ESCAPE.search(decoded) or _contains_sensitive_text(decoded):
        raise ValueError(f"{field_name} contains sensitive content")


def _contains_sensitive_text(value: str) -> bool:
    return bool(
        _DATA_URL.search(value)
        or _BEARER_CREDENTIAL.search(value)
        or _LABELED_CREDENTIAL.search(value)
        or _contains_sensitive_url_query(value)
        or _contains_sensitive_quoted_label(value)
        or _contains_bare_base64(value)
    )


def _is_sensitive_key(value: str) -> bool:
    normalized = _normalize_key(value)
    segments = tuple(segment for segment in normalized.split("_") if segment)
    if not segments:
        return False
    if value in _SAFE_RESOURCE_TOKEN_KEYS:
        return False
    compact = "".join(segments)
    if any(
        segment in _SENSITIVE_COMPACT_KEY_MARKERS for segment in segments
    ) or any(
        compact.endswith(marker) for marker in _SENSITIVE_COMPACT_KEY_MARKERS
    ):
        return True
    if (
        "token" in segments
        or compact.endswith("token")
        or "secret" in segments
        or compact.endswith("secret")
        or "jwt" in segments
    ):
        return True
    if any(segment in _SENSITIVE_SINGLE_KEY_SEGMENTS for segment in segments):
        return True
    if "payload" in segments or segments in {("event",), ("raw",)}:
        return True
    if "event" in segments and bool(
        {"body", "data", "json", "raw"} & set(segments)
    ):
        return True
    if any(pair in _SENSITIVE_KEY_PAIRS for pair in zip(segments, segments[1:])):
        return True
    return "raw" in segments and bool({"body", "event", "payload"} & set(segments))


def _contains_sensitive_quoted_label(value: str) -> bool:
    return any(
        _is_sensitive_key(match.group(1))
        for match in _QUOTED_LABEL.finditer(value)
    )


def _decode_json_text_layer(value: str) -> str:
    try:
        decoded = json.loads(f'"{value}"')
    except (json.JSONDecodeError, RecursionError, TypeError, ValueError):
        decoded = value
    if not isinstance(decoded, str):
        decoded = value
    decoded = _JSON_UNICODE_ESCAPE.sub(
        lambda match: chr(int(match.group(1), 16)),
        decoded,
    )
    return _JSON_SOLIDUS_ESCAPE.sub("/", decoded)


def _contains_bare_base64(value: str) -> bool:
    for pattern in (_BASE64_CANDIDATE, _WRAPPED_BASE64_CANDIDATE):
        for match in pattern.finditer(value):
            encoded = re.sub(r"[ \t\r\n]+", "", match.group(0))
            padded = encoded + "=" * (-len(encoded) % 4)
            try:
                decoded = base64.b64decode(
                    padded,
                    altchars=b"-_",
                    validate=True,
                )
            except (binascii.Error, ValueError):
                continue
            if _has_image_magic(decoded):
                return True
            if (
                len(encoded) >= 128
                and len(decoded) >= 96
                and _entropy(encoded) >= 4.5
            ):
                return True
    return False


def _has_image_magic(value: bytes) -> bool:
    return (
        value.startswith(b"\x89PNG\r\n\x1a\n")
        or value.startswith(b"\xff\xd8\xff")
        or value.startswith((b"GIF87a", b"GIF89a"))
        or value.startswith(b"BM")
        or value.startswith((b"II*\x00", b"MM\x00*"))
        or (
            len(value) >= 12
            and value.startswith(b"RIFF")
            and value[8:12] == b"WEBP"
        )
    )


def _entropy(value: str) -> float:
    symbols = value.rstrip("=")
    if not symbols:
        return 0.0
    counts = Counter(symbols)
    length = len(symbols)
    return -sum(
        (count / length) * log2(count / length) for count in counts.values()
    )


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
        or key in _SAFE_RESOURCE_TOKEN_KEYS
        or _is_sensitive_key(key)
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


async def _commit_durably(connection: aiosqlite.Connection) -> None:
    await _await_sqlite_operation_durably(connection.commit())


async def _rollback_durably(connection: aiosqlite.Connection) -> None:
    await _await_sqlite_operation_durably(connection.rollback())


async def _await_sqlite_operation_durably(operation: Any) -> None:
    operation_task = asyncio.create_task(operation)
    while True:
        try:
            await asyncio.shield(operation_task)
            return
        except asyncio.CancelledError:
            if operation_task.done():
                operation_task.result()
                return
