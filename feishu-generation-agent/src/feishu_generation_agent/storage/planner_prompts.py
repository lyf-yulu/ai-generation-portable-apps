import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite


_SCHEMA = """
CREATE TABLE IF NOT EXISTS planner_prompt_profiles (
  portal_user_id TEXT PRIMARY KEY,
  username TEXT NOT NULL,
  prompt_text TEXT NOT NULL,
  version INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
"""
_MAX_PROMPT_LENGTH = 20_000


@dataclass(frozen=True, slots=True)
class PlannerPromptProfile:
    portal_user_id: str
    username: str
    prompt_text: str
    version: int
    created_at: str
    updated_at: str


class PlannerPromptStore:
    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection
        self._lock = asyncio.Lock()

    @classmethod
    async def open(cls, path: Path) -> "PlannerPromptStore":
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = await aiosqlite.connect(str(path), isolation_level=None)
        connection.row_factory = aiosqlite.Row
        try:
            await connection.execute("PRAGMA busy_timeout = 5000")
            await connection.executescript(_SCHEMA)
            await connection.commit()
        except BaseException:
            await connection.close()
            raise
        return cls(connection)

    async def get(self, portal_user_id: str) -> PlannerPromptProfile | None:
        cursor = await self._connection.execute(
            """SELECT portal_user_id, username, prompt_text, version, created_at, updated_at
            FROM planner_prompt_profiles WHERE portal_user_id = ?""",
            (portal_user_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return _profile_from_row(row) if row is not None else None

    async def save(
        self,
        *,
        portal_user_id: str,
        username: str,
        prompt_text: str,
    ) -> PlannerPromptProfile:
        if not prompt_text.strip():
            raise ValueError("prompt_text must not be blank")
        if len(prompt_text) > _MAX_PROMPT_LENGTH:
            raise ValueError("prompt_text must not exceed 20,000 characters")

        updated_at = _utc_now()
        async with self._lock:
            try:
                await self._connection.execute("BEGIN IMMEDIATE")
                cursor = await self._connection.execute(
                    """SELECT version, created_at FROM planner_prompt_profiles
                    WHERE portal_user_id = ?""",
                    (portal_user_id,),
                )
                existing = await cursor.fetchone()
                await cursor.close()
                if existing is None:
                    version = 1
                    created_at = updated_at
                    await self._connection.execute(
                        """INSERT INTO planner_prompt_profiles (
                          portal_user_id, username, prompt_text, version, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?)""",
                        (
                            portal_user_id,
                            username,
                            prompt_text,
                            version,
                            created_at,
                            updated_at,
                        ),
                    )
                else:
                    version = existing["version"] + 1
                    created_at = existing["created_at"]
                    await self._connection.execute(
                        """UPDATE planner_prompt_profiles SET
                          username = ?, prompt_text = ?, version = ?, updated_at = ?
                        WHERE portal_user_id = ?""",
                        (
                            username,
                            prompt_text,
                            version,
                            updated_at,
                            portal_user_id,
                        ),
                    )
                await self._connection.commit()
                profile = PlannerPromptProfile(
                    portal_user_id=portal_user_id,
                    username=username,
                    prompt_text=prompt_text,
                    version=version,
                    created_at=created_at,
                    updated_at=updated_at,
                )
            except BaseException:
                await self._connection.rollback()
                raise
        return profile

    async def delete(self, portal_user_id: str) -> bool:
        async with self._lock:
            cursor = await self._connection.execute(
                "DELETE FROM planner_prompt_profiles WHERE portal_user_id = ?",
                (portal_user_id,),
            )
            await self._connection.commit()
            deleted = cursor.rowcount > 0
            await cursor.close()
        return deleted

    async def close(self) -> None:
        await self._connection.close()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _profile_from_row(row: aiosqlite.Row) -> PlannerPromptProfile:
    return PlannerPromptProfile(
        portal_user_id=row["portal_user_id"],
        username=row["username"],
        prompt_text=row["prompt_text"],
        version=row["version"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
