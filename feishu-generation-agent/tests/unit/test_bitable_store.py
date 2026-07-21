import asyncio
import json
import sqlite3
import subprocess
import sys

import aiosqlite
import pytest

from feishu_generation_agent.domain.bitable import BitableBinding, TableTaskStatus
from feishu_generation_agent.storage.bitable_tasks import (
    BitableTaskStore,
    TaskAlreadyClaimed,
)


def test_bitable_store_imports_in_a_clean_python_process():
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from feishu_generation_agent.storage.bitable_tasks "
                "import BitableTaskStore; "
                "from feishu_generation_agent.storage import "
                "FileStore, ProviderResultStore, Repository, "
                "StagedProviderResult, StoredFile; "
                "print(BitableTaskStore.__name__, FileStore.__name__, "
                "ProviderResultStore.__name__, Repository.__name__, "
                "StagedProviderResult.__name__, StoredFile.__name__)"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == (
        "BitableTaskStore FileStore ProviderResultStore Repository "
        "StagedProviderResult StoredFile"
    )


async def _claim(
    store: BitableTaskStore,
    *,
    record_id: str = "rec",
    run_id: str = "run-1",
    thread_id: str = "thread-1",
) -> BitableBinding:
    return await store.claim(
        app_token="app",
        table_id="tbl",
        view_id="vew",
        record_id=record_id,
        source_url="https://x.feishu.cn/docx/doc",
        display_text="1",
        claimant_open_id="ou_a",
        run_id=run_id,
        thread_id=thread_id,
        reply_context={"message_id": "om_1"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("attempt", range(10))
async def test_two_connections_cannot_claim_same_record(tmp_path, attempt):
    database_path = tmp_path / f"agent-{attempt}.sqlite3"
    stores = [
        await BitableTaskStore.open(database_path),
        await BitableTaskStore.open(database_path),
    ]
    ready = 0
    ready_lock = asyncio.Lock()
    start = asyncio.Event()

    async def claim(store, open_id):
        nonlocal ready
        async with ready_lock:
            ready += 1
            if ready == len(stores):
                start.set()
        await start.wait()
        return await store.claim(
            app_token="app",
            table_id="tbl",
            view_id="vew",
            record_id="rec",
            source_url="https://x.feishu.cn/docx/doc",
            display_text="1",
            claimant_open_id=open_id,
            run_id=f"run-{open_id}",
            thread_id=f"thread-{open_id}",
            reply_context={},
        )

    try:
        results = await asyncio.gather(
            claim(stores[0], f"ou_a_{attempt}"),
            claim(stores[1], f"ou_b_{attempt}"),
            return_exceptions=True,
        )

        assert sum(isinstance(item, BitableBinding) for item in results) == 1
        assert sum(isinstance(item, TaskAlreadyClaimed) for item in results) == 1
    finally:
        await asyncio.gather(*(store.close() for store in stores))


@pytest.mark.asyncio
async def test_ingress_id_is_accepted_once(tmp_path):
    store = await BitableTaskStore.open(tmp_path / "agent.sqlite3")
    try:
        assert await store.accept_ingress(
            dedupe_id="event-1",
            kind="approve",
            command={"run_id": "run-1", "approval_version": 2},
        )
        assert not await store.accept_ingress(
            dedupe_id="event-1",
            kind="approve",
            command={"run_id": "run-1", "approval_version": 2},
        )
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_action_id_is_accepted_once(tmp_path):
    store = await BitableTaskStore.open(tmp_path / "agent.sqlite3")
    try:
        assert await store.accept_action(
            action_id="action-1",
            kind="approve",
            command={"run_id": "run-1", "approval_version": 2},
        )
        assert not await store.accept_action(
            action_id="action-1",
            kind="approve",
            command={"run_id": "run-1", "approval_version": 2},
        )
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_claim_can_be_read_by_record_and_run(tmp_path):
    store = await BitableTaskStore.open(tmp_path / "agent.sqlite3")
    try:
        claimed = await _claim(store)

        assert claimed.status is TableTaskStatus.PROCESSING
        assert claimed.approval_version == 0
        assert claimed.reply_context == {"message_id": "om_1"}
        assert await store.get_by_record("app", "tbl", "rec") == claimed
        assert await store.get_by_run("run-1") == claimed
        assert await store.get_by_record("app", "tbl", "missing") is None
        assert await store.get_by_run("missing") is None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_terminal_status_does_not_release_claim_implicitly(tmp_path):
    store = await BitableTaskStore.open(tmp_path / "agent.sqlite3")
    try:
        await _claim(store)
        updated = await store.set_status(
            "run-1", TableTaskStatus.COMPLETED, last_error="writeback pending"
        )

        assert updated.status is TableTaskStatus.COMPLETED
        assert updated.last_error == "writeback pending"
        with pytest.raises(TaskAlreadyClaimed):
            await _claim(store, run_id="run-2", thread_id="thread-2")
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_release_allows_a_fresh_claim_and_resets_approval(tmp_path):
    store = await BitableTaskStore.open(tmp_path / "agent.sqlite3")
    try:
        await _claim(store)
        await store.advance_approval("run-1", "plan-a")
        released = await store.release(
            "run-1", status=TableTaskStatus.FAILED, last_error="provider failed"
        )
        reclaimed = await _claim(store, run_id="run-2", thread_id="thread-2")

        assert released.status is TableTaskStatus.FAILED
        assert released.last_error == "provider failed"
        assert reclaimed.run_id == "run-2"
        assert reclaimed.status is TableTaskStatus.PROCESSING
        assert reclaimed.approval_version == 0
        assert reclaimed.plan_fingerprint is None
        assert reclaimed.last_error is None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_release_rejects_non_releasable_status(tmp_path):
    store = await BitableTaskStore.open(tmp_path / "agent.sqlite3")
    try:
        await _claim(store)

        with pytest.raises(ValueError, match="release status"):
            await store.release("run-1", status=TableTaskStatus.GENERATING)
        with pytest.raises(TaskAlreadyClaimed):
            await _claim(store, run_id="run-2", thread_id="thread-2")
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_unknown_run_cannot_be_updated_or_released(tmp_path):
    store = await BitableTaskStore.open(tmp_path / "agent.sqlite3")
    try:
        with pytest.raises(KeyError, match="missing"):
            await store.set_status("missing", TableTaskStatus.FAILED)
        with pytest.raises(KeyError, match="missing"):
            await store.release("missing", status=TableTaskStatus.FAILED)
        with pytest.raises(KeyError, match="missing"):
            await store.advance_approval("missing", "plan-a")
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_approval_version_only_advances_for_a_new_fingerprint(tmp_path):
    store = await BitableTaskStore.open(tmp_path / "agent.sqlite3")
    try:
        await _claim(store)

        first = await store.advance_approval("run-1", "plan-a")
        replay = await store.advance_approval("run-1", "plan-a")
        changed = await store.advance_approval("run-1", "plan-b")

        assert first.approval_version == 1
        assert replay.approval_version == 1
        assert changed.approval_version == 2
        assert changed.plan_fingerprint == "plan-b"
    finally:
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("accept_name", "finish_name", "id_name", "item_id"),
    [
        ("accept_ingress", "finish_ingress", "dedupe_id", "event-1"),
        ("accept_action", "finish_action", "action_id", "action-1"),
    ],
)
async def test_finish_is_first_terminal_result_wins(
    tmp_path, accept_name, finish_name, id_name, item_id
):
    database_path = tmp_path / "agent.sqlite3"
    store = await BitableTaskStore.open(database_path)
    try:
        accept = getattr(store, accept_name)
        finish = getattr(store, finish_name)
        assert await accept(
            **{
                id_name: item_id,
                "kind": "approve",
                "command": {"run_id": "run-1", "approval_version": 2},
            }
        )

        assert await finish(
            item_id,
            status="completed",
            result={"accepted": True},
        )
        assert not await finish(
            item_id,
            status="failed",
            result={"accepted": False},
        )
    finally:
        await store.close()

    table = "bot_ingress" if id_name == "dedupe_id" else "card_actions"
    id_column = id_name
    with sqlite3.connect(database_path) as connection:
        status, result_json = connection.execute(
            f"SELECT status, result_json FROM {table} WHERE {id_column} = ?",
            (item_id,),
        ).fetchone()
    assert status == "completed"
    assert json.loads(result_json) == {"accepted": True}


@pytest.mark.asyncio
async def test_finish_validates_status_result_and_missing_id(tmp_path):
    store = await BitableTaskStore.open(tmp_path / "agent.sqlite3")
    try:
        assert not await store.finish_ingress("missing", status="completed")
        with pytest.raises(ValueError, match="finish status"):
            await store.finish_action("missing", status="pending")
        with pytest.raises(TypeError, match="result must be a JSON object"):
            await store.finish_ingress(
                "missing", status="failed", result=[]  # type: ignore[arg-type]
            )
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_first_finish_requires_result_object_and_keeps_pending_on_error(tmp_path):
    store = await BitableTaskStore.open(tmp_path / "agent.sqlite3")
    try:
        assert await store.accept_ingress(
            dedupe_id="event-1",
            kind="approve",
            command={"run_id": "run-1"},
        )

        with pytest.raises(TypeError, match="result must be a JSON object"):
            await store.finish_ingress("event-1", status="completed")
        assert await store.finish_ingress(
            "event-1",
            status="completed",
            result={"accepted": True},
        )
        assert not await store.finish_ingress("event-1", status="completed")
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_commands_must_be_json_objects(tmp_path):
    store = await BitableTaskStore.open(tmp_path / "agent.sqlite3")
    try:
        with pytest.raises(TypeError, match="command must be a JSON object"):
            await store.accept_ingress(
                dedupe_id="event-list",
                kind="approve",
                command=[]  # type: ignore[arg-type]
            )
        with pytest.raises(ValueError, match="command must be JSON serializable"):
            await store.accept_action(
                action_id="action-bytes",
                kind="approve",
                command={"run_id": b"secret"},
            )
    finally:
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        {"raw_event": {"event_id": "evt-1"}},
        {"context": {"Authorization": "Bearer fictional-secret"}},
        {"context": [{"tenant_access_token": "fictional-token"}]},
        {"api_key": "fictional-key"},
        {"user_access_token": "fictional-token"},
        {"appAccessToken": "fictional-token"},
        {"event": {"sender": {"open_id": "ou_1"}, "text": "raw"}},
        {"payload": {"action": {"value": {"run_id": "run-1"}}}},
        {"auth": "Bearer fictional-secret"},
        {"callback": "https://files.example/x?X-Amz-Signature=fictional"},
        {"download": "https://files.example/x?credential=fictional"},
        {"media": ["https://files.example/x?access_token=fictional"]},
        {
            "feedback": (
                "请参考 [临时文件](https://files.example/x?"
                "X-Amz-Signature=fictional)"
            )
        },
    ],
)
async def test_commands_reject_sensitive_nested_content(tmp_path, command):
    store = await BitableTaskStore.open(tmp_path / "agent.sqlite3")
    try:
        with pytest.raises(ValueError, match="sensitive"):
            await store.accept_action(
                action_id="action-sensitive",
                kind="approve",
                command=command,
            )
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_commands_allow_minimal_ids_feedback_and_unsigned_urls(tmp_path):
    store = await BitableTaskStore.open(tmp_path / "agent.sqlite3")
    try:
        assert await store.accept_action(
            action_id="action-safe",
            kind="revise",
            command={
                "run_id": "run-1",
                "record_id": "rec-1",
                "app_token": "app-resource-id",
                "actor_open_id": "ou_1",
                "selected_task_ids": ["task-1"],
                "feedback": "改成暖色",
                "chat_id": "oc_1",
                "message_id": "om_1",
                "source_url": "https://tenant.feishu.cn/docx/doc?view=compact",
            },
        )
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_commands_reject_oversized_json(tmp_path):
    store = await BitableTaskStore.open(tmp_path / "agent.sqlite3")
    try:
        with pytest.raises(ValueError, match="too large"):
            await store.accept_ingress(
                dedupe_id="event-large",
                kind="message",
                command={"feedback": "x" * (64 * 1024)},
            )
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_schema_contains_only_minimal_task_and_dedupe_fields(tmp_path):
    database_path = tmp_path / "agent.sqlite3"
    store = await BitableTaskStore.open(database_path)
    await store.close()

    expected_columns = {
        "bitable_tasks": {
            "app_token", "table_id", "record_id", "view_id", "source_url",
            "display_text", "run_id", "thread_id", "claimant_open_id", "status",
            "approval_version", "plan_fingerprint", "reply_context_json",
            "last_error", "active", "created_at", "updated_at",
        },
        "bot_ingress": {
            "dedupe_id", "kind", "command_json", "status", "result_json",
            "created_at", "updated_at",
        },
        "card_actions": {
            "action_id", "kind", "command_json", "status", "result_json",
            "created_at", "updated_at",
        },
    }
    with sqlite3.connect(database_path) as connection:
        actual_columns = {
            table: {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            for table in expected_columns
        }

    assert actual_columns == expected_columns


@pytest.mark.asyncio
async def test_binding_and_dedupe_survive_store_reopen(tmp_path):
    database_path = tmp_path / "agent.sqlite3"
    store = await BitableTaskStore.open(database_path)
    try:
        claimed = await _claim(store)
        assert await store.accept_ingress(
            dedupe_id="event-1",
            kind="claim",
            command={"run_id": claimed.run_id},
        )
    finally:
        await store.close()

    reopened = await BitableTaskStore.open(database_path)
    try:
        assert await reopened.get_by_run("run-1") == claimed
        assert not await reopened.accept_ingress(
            dedupe_id="event-1",
            kind="claim",
            command={"run_id": claimed.run_id},
        )
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_cancelled_claim_waiting_for_write_lock_leaves_store_usable(tmp_path):
    database_path = tmp_path / "agent.sqlite3"
    store = await BitableTaskStore.open(database_path)
    blocker = await aiosqlite.connect(str(database_path), isolation_level=None)
    await blocker.execute("BEGIN IMMEDIATE")
    waiting_claim = asyncio.create_task(_claim(store))
    await asyncio.sleep(0.01)

    try:
        waiting_claim.cancel()
        await blocker.rollback()
        with pytest.raises(asyncio.CancelledError):
            await waiting_claim
        await store._connection.execute("SELECT 1")

        assert not store._connection.in_transaction

        claimed = await _claim(
            store,
            record_id="rec-after-cancel",
            run_id="run-after-cancel",
            thread_id="thread-after-cancel",
        )
        assert claimed.record_id == "rec-after-cancel"
    finally:
        await blocker.rollback()
        await blocker.close()
        await store.close()
