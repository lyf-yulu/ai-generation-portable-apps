from pathlib import Path

import aiosqlite
import pytest

from feishu_generation_agent.domain.bitable import BitableLocation
from feishu_generation_agent.domain.production_bitable import (
    ProductionSourceSnapshot,
    ProductionTaskSummary,
    ResultTableTarget,
)
from feishu_generation_agent.storage.production_tasks import (
    ProductionTaskAlreadyClaimed,
    ProductionTaskStore,
)


def _location() -> BitableLocation:
    return BitableLocation(
        wiki_token="wikiProd",
        app_token="appProd",
        table_id="tblProd",
        view_id="vewProd",
        source_url="https://tenant.feishu.cn/wiki/wikiProd?table=tblProd&view=vewProd",
    )


def _task() -> ProductionTaskSummary:
    return ProductionTaskSummary(
        record_id="recProd",
        display_text="需求 A",
        source_url="https://tenant.feishu.cn/docx/docA",
        progress="未开始",
        maker_open_id="ou-maker",
        maker_name="制作人",
        snapshot=ProductionSourceSnapshot(
            requirement_name="需求 A",
            requirement_attachment="https://tenant.feishu.cn/docx/docA",
            project_names=["项目 A"],
            requester_open_ids=["ou-requester"],
            requester_names=["发起人"],
            maker_open_ids=["ou-maker"],
            maker_names=["制作人"],
        ),
    )


async def test_claim_is_unique_per_source_record_and_persists_snapshot(
    tmp_path: Path,
) -> None:
    store = await ProductionTaskStore.open(tmp_path / "production.sqlite3")
    try:
        binding = await store.claim(
            _location(),
            _task(),
            run_id="run-1",
            thread_id="thread-1",
            owner_user_id="user-a",
        )

        assert binding.owner_user_id == "user-a"
        assert binding.snapshot.requirement_name == "需求 A"
        with pytest.raises(ProductionTaskAlreadyClaimed):
            await store.claim(
                _location(),
                _task(),
                run_id="run-2",
                thread_id="thread-2",
                owner_user_id="user-b",
            )
        assert [
            item.run_id
            for item in await store.list_active(
                "appProd", "tblProd", owner_user_id="user-a"
            )
        ] == ["run-1"]
        assert (
            await store.list_active(
                "appProd", "tblProd", owner_user_id="user-b"
            )
            == []
        )
    finally:
        await store.close()


async def test_result_target_and_delivery_row_survive_reopen(tmp_path: Path) -> None:
    path = tmp_path / "production.sqlite3"
    store = await ProductionTaskStore.open(path)
    try:
        await store.claim(_location(), _task(), run_id="run-1", thread_id="thread-1")
        await store.upsert_result_target(
            ResultTableTarget(
                maker_open_id="ou-maker",
                maker_name="制作人",
                app_token="app-result",
                table_id="tbl-result",
                url="https://tenant.feishu.cn/base/app-result",
            )
        )
        await store.reserve_delivery("run-1")
        await store.complete_delivery("run-1", result_record_id="rec-result")
    finally:
        await store.close()

    reopened = await ProductionTaskStore.open(path)
    try:
        assert (await reopened.get_result_target("ou-maker")).table_id == "tbl-result"
        assert (await reopened.get_delivery("run-1")).result_record_id == "rec-result"
    finally:
        await reopened.close()


async def test_production_store_migrates_legacy_active_and_history_owners(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy-production.sqlite3"
    async with aiosqlite.connect(path) as connection:
        await connection.executescript(
            """
            CREATE TABLE production_tasks (
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
              PRIMARY KEY (
                source_app_token, source_table_id, source_record_id
              )
            );
            CREATE TABLE production_task_history (
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
              run_id TEXT NOT NULL PRIMARY KEY,
              thread_id TEXT NOT NULL UNIQUE,
              status TEXT NOT NULL,
              last_error TEXT,
              active INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            """
        )

    store = await ProductionTaskStore.open(path)
    await store.close()
    reopened = await ProductionTaskStore.open(path)
    await reopened.close()

    async with aiosqlite.connect(path) as connection:
        for table in ("production_tasks", "production_task_history"):
            rows = await (
                await connection.execute(f"PRAGMA table_info({table})")
            ).fetchall()
            owner = next(row for row in rows if row[1] == "owner_user_id")
            assert owner[3] == 1
            assert owner[4] == "'prime-local'"


async def test_recent_history_survives_mixed_legacy_and_fresh_column_order(
    tmp_path: Path,
) -> None:
    path = tmp_path / "mixed-production.sqlite3"
    async with aiosqlite.connect(path) as connection:
        await connection.execute(
            """
            CREATE TABLE production_tasks (
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
              PRIMARY KEY (
                source_app_token, source_table_id, source_record_id
              )
            )
            """
        )

    store = await ProductionTaskStore.open(path)
    try:
        first = await store.claim(
            _location(),
            _task(),
            run_id="run-first",
            thread_id="thread-first",
            owner_user_id="user-a",
        )
        from feishu_generation_agent.domain.bitable import TableTaskStatus

        await store.release(
            first.run_id,
            status=TableTaskStatus.COMPLETED,
            owner_user_id="user-a",
        )
        await store.claim(
            _location(),
            _task(),
            run_id="run-second",
            thread_id="thread-second",
            owner_user_id="user-b",
        )

        recent = await store.list_recent(
            "appProd", "tblProd", owner_user_id="user-a"
        )
        assert [(item.run_id, item.owner_user_id) for item in recent] == [
            ("run-first", "user-a")
        ]
    finally:
        await store.close()
