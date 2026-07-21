import asyncio
import json
from pathlib import Path

import aiosqlite

from feishu_generation_agent.domain.bitable import BitableLocation, TableTaskStatus
from feishu_generation_agent.domain.production_bitable import (
    ProductionBinding,
    ProductionDelivery,
    ProductionTaskSummary,
    ResultTableTarget,
)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS production_tasks (
  source_app_token TEXT NOT NULL,
  source_table_id TEXT NOT NULL,
  source_record_id TEXT NOT NULL,
  source_location_json TEXT NOT NULL,
  source_url TEXT NOT NULL,
  display_text TEXT NOT NULL,
  progress TEXT NOT NULL,
  maker_open_id TEXT,
  maker_name TEXT,
  snapshot_json TEXT NOT NULL,
  run_id TEXT NOT NULL UNIQUE,
  thread_id TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL,
  last_error TEXT,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (source_app_token, source_table_id, source_record_id)
);
CREATE TABLE IF NOT EXISTS maker_result_tables (
  maker_open_id TEXT PRIMARY KEY,
  maker_name TEXT NOT NULL,
  app_token TEXT NOT NULL,
  table_id TEXT NOT NULL,
  url TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS production_deliveries (
  run_id TEXT PRIMARY KEY,
  result_record_id TEXT,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
"""


class ProductionTaskAlreadyClaimed(RuntimeError):
    pass


class ProductionTaskStore:
    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection
        self._lock = asyncio.Lock()

    @classmethod
    async def open(cls, path: str | Path) -> "ProductionTaskStore":
        database_path = Path(path)
        database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = await aiosqlite.connect(str(database_path), isolation_level=None)
        connection.row_factory = aiosqlite.Row
        await connection.execute("PRAGMA busy_timeout = 5000")
        await connection.executescript(_SCHEMA)
        return cls(connection)

    async def close(self) -> None:
        await self._connection.close()

    async def claim(
        self,
        location: BitableLocation,
        task: ProductionTaskSummary,
        *,
        run_id: str,
        thread_id: str,
    ) -> ProductionBinding:
        app_token = _app_token(location)
        async with self._lock:
            await self._connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = await self._connection.execute(
                    "SELECT active FROM production_tasks WHERE source_app_token = ? AND source_table_id = ? AND source_record_id = ?",
                    (app_token, location.table_id, task.record_id),
                )
                existing = await cursor.fetchone()
                await cursor.close()
                if existing is not None and existing["active"] == 1:
                    raise ProductionTaskAlreadyClaimed("生产表任务已被领取")
                payload = (
                    json.dumps(location.model_dump(mode="json"), ensure_ascii=False),
                    task.source_url,
                    task.display_text,
                    task.progress,
                    task.maker_open_id,
                    task.maker_name,
                    json.dumps(task.snapshot.model_dump(mode="json"), ensure_ascii=False),
                    run_id,
                    thread_id,
                    TableTaskStatus.PROCESSING.value,
                )
                if existing is None:
                    await self._connection.execute(
                        """INSERT INTO production_tasks (
                          source_app_token, source_table_id, source_record_id,
                          source_location_json, source_url, display_text, progress,
                          maker_open_id, maker_name, snapshot_json, run_id, thread_id,
                          status, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
                        (app_token, location.table_id, task.record_id, *payload),
                    )
                else:
                    await self._connection.execute(
                        """UPDATE production_tasks SET
                          source_location_json = ?, source_url = ?, display_text = ?, progress = ?,
                          maker_open_id = ?, maker_name = ?, snapshot_json = ?, run_id = ?,
                          thread_id = ?, status = ?, last_error = NULL, active = 1,
                          updated_at = CURRENT_TIMESTAMP
                        WHERE source_app_token = ? AND source_table_id = ? AND source_record_id = ?""",
                        (*payload, app_token, location.table_id, task.record_id),
                    )
                binding = await self._get_by_run_locked(run_id)
                await self._connection.commit()
            except BaseException:
                await self._connection.rollback()
                raise
        assert binding is not None
        return binding

    async def get_by_run(self, run_id: str) -> ProductionBinding | None:
        async with self._lock:
            return await self._get_by_run_locked(run_id)

    async def get_result_target(self, maker_open_id: str) -> ResultTableTarget | None:
        async with self._lock:
            cursor = await self._connection.execute(
                "SELECT * FROM maker_result_tables WHERE maker_open_id = ?", (maker_open_id,)
            )
            row = await cursor.fetchone()
            await cursor.close()
        if row is None:
            return None
        return ResultTableTarget(
            maker_open_id=row["maker_open_id"], maker_name=row["maker_name"],
            app_token=row["app_token"], table_id=row["table_id"], url=row["url"],
        )

    async def upsert_result_target(self, target: ResultTableTarget) -> None:
        async with self._lock:
            await self._connection.execute(
                """INSERT INTO maker_result_tables (
                  maker_open_id, maker_name, app_token, table_id, url, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(maker_open_id) DO UPDATE SET
                  maker_name = excluded.maker_name, app_token = excluded.app_token,
                  table_id = excluded.table_id, url = excluded.url,
                  updated_at = CURRENT_TIMESTAMP""",
                (target.maker_open_id, target.maker_name, target.app_token, target.table_id, target.url),
            )
            await self._connection.commit()

    async def reserve_delivery(self, run_id: str) -> ProductionDelivery:
        async with self._lock:
            await self._connection.execute(
                """INSERT INTO production_deliveries (run_id, status, created_at, updated_at)
                VALUES (?, 'pending', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(run_id) DO NOTHING""",
                (run_id,),
            )
            await self._connection.commit()
            return await self._get_delivery_locked(run_id)

    async def complete_delivery(self, run_id: str, *, result_record_id: str) -> ProductionDelivery:
        async with self._lock:
            await self._connection.execute(
                """UPDATE production_deliveries SET result_record_id = ?, status = 'succeeded',
                updated_at = CURRENT_TIMESTAMP WHERE run_id = ?""",
                (result_record_id, run_id),
            )
            await self._connection.commit()
            return await self._get_delivery_locked(run_id)

    async def get_delivery(self, run_id: str) -> ProductionDelivery | None:
        async with self._lock:
            cursor = await self._connection.execute(
                "SELECT * FROM production_deliveries WHERE run_id = ?", (run_id,)
            )
            row = await cursor.fetchone()
            await cursor.close()
        return _delivery_from_row(row) if row is not None else None

    async def _get_by_run_locked(self, run_id: str) -> ProductionBinding | None:
        cursor = await self._connection.execute(
            "SELECT * FROM production_tasks WHERE run_id = ?", (run_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return _binding_from_row(row) if row is not None else None

    async def _get_delivery_locked(self, run_id: str) -> ProductionDelivery:
        cursor = await self._connection.execute(
            "SELECT * FROM production_deliveries WHERE run_id = ?", (run_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        assert row is not None
        return _delivery_from_row(row)


def _app_token(location: BitableLocation) -> str:
    if not location.app_token:
        raise ValueError("production location is unresolved")
    return location.app_token


def _binding_from_row(row: aiosqlite.Row) -> ProductionBinding:
    return ProductionBinding(
        source_location=BitableLocation.model_validate(json.loads(row["source_location_json"])),
        record_id=row["source_record_id"], source_url=row["source_url"],
        display_text=row["display_text"], progress=row["progress"],
        maker_open_id=row["maker_open_id"], maker_name=row["maker_name"],
        snapshot=json.loads(row["snapshot_json"]), run_id=row["run_id"],
        thread_id=row["thread_id"], status=TableTaskStatus(row["status"]),
        last_error=row["last_error"],
    )


def _delivery_from_row(row: aiosqlite.Row) -> ProductionDelivery:
    return ProductionDelivery(
        run_id=row["run_id"], result_record_id=row["result_record_id"], status=row["status"]
    )
