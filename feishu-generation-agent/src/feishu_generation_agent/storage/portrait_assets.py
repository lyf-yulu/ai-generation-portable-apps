from pathlib import Path

import aiosqlite


_SCHEMA = """
CREATE TABLE IF NOT EXISTS portrait_runs (
  run_id TEXT PRIMARY KEY,
  group_id TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS portrait_assets (
  run_id TEXT NOT NULL,
  source_asset_id TEXT NOT NULL,
  volcengine_asset_id TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (run_id, source_asset_id)
);
"""


class PortraitAssetStore:
    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection

    @classmethod
    async def open(cls, path: str | Path) -> "PortraitAssetStore":
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = await aiosqlite.connect(str(path))
        connection.row_factory = aiosqlite.Row
        await connection.executescript(_SCHEMA)
        await connection.commit()
        return cls(connection)

    async def close(self) -> None:
        await self._connection.close()

    async def get_group_id(self, run_id: str) -> str | None:
        cursor = await self._connection.execute(
            "SELECT group_id FROM portrait_runs WHERE run_id = ?", (run_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row["group_id"] if row is not None else None

    async def save_group_id(self, run_id: str, group_id: str) -> None:
        await self._connection.execute(
            "INSERT OR IGNORE INTO portrait_runs (run_id, group_id) VALUES (?, ?)",
            (run_id, group_id),
        )
        await self._connection.commit()

    async def get_asset(self, run_id: str, source_asset_id: str) -> tuple[str, str] | None:
        cursor = await self._connection.execute(
            """SELECT volcengine_asset_id, status FROM portrait_assets
            WHERE run_id = ? AND source_asset_id = ?""",
            (run_id, source_asset_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        return row["volcengine_asset_id"], row["status"]

    async def save_asset(
        self, run_id: str, source_asset_id: str, volcengine_asset_id: str, status: str
    ) -> None:
        await self._connection.execute(
            """INSERT INTO portrait_assets (
              run_id, source_asset_id, volcengine_asset_id, status
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(run_id, source_asset_id) DO UPDATE SET
              volcengine_asset_id = excluded.volcengine_asset_id,
              status = excluded.status,
              updated_at = CURRENT_TIMESTAMP""",
            (run_id, source_asset_id, volcengine_asset_id, status),
        )
        await self._connection.commit()
