import asyncio
import base64
import json
import sqlite3
import subprocess
import sys

import aiosqlite
import pytest

import feishu_generation_agent.storage.bitable_tasks as bitable_tasks_module
from feishu_generation_agent.domain.bitable import BitableBinding, TableTaskStatus
from feishu_generation_agent.storage.bitable_tasks import (
    BitableTaskStore,
    TaskAlreadyClaimed,
)


PLAN_FINGERPRINT_A = "a" * 64
PLAN_FINGERPRINT_B = "b" * 64
BMP_1X1_BASE64 = (
    "Qk1CAAAAAAAAAD4AAAAoAAAAAQAAAAEAAAABAAEAAAAAAAQAAADEDgAAxA4AAAIAAA"
    "ACAAAAAAAAAP///wAAAAAA"
)
TIFF_II_1X1_BASE64 = (
    "SUkqAAgAAAAIAAABBAABAAAAAQAAAAEBBAABAAAAAQAAAAMBAwABAAAAAQAAABYBAw"
    "ABAAAAAQAAABEBBAABAAAAbgAAABYBBAABAAAAAQAAABcBBAABAAAAAQAAABwBAwAB"
    "AAAAAQAAAAAAAAAA"
)
TIFF_MM_MAGIC_BASE64 = base64.b64encode(
    b"MM\x00*\x00\x00\x00\x08\x00\x00\x00\x00"
).decode()


def _wrap_base64(value: str, width: int = 12) -> str:
    return " ".join(
        value[index : index + width]
        for index in range(0, len(value), width)
    )


def _split_base64_once(value: str) -> str:
    return f"{value[:12]}\n{value[12:]}"


def _split_base64_in_half(value: str) -> str:
    midpoint = (len(value) // 8) * 4
    return f"{value[:midpoint]}\n{value[midpoint:]}"


def _deeply_escaped_sensitive_json() -> str:
    label = r"\u0074enant_access_token"
    for _ in range(13):
        label = label.replace("\\", r"\u005c")
    return f'{{"{label}":"fictional-token"}}'


def _escape_json_text(value: str, layers: int) -> str:
    for _ in range(layers):
        value = json.dumps(value, ensure_ascii=True)[1:-1]
    return value


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (
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
            (
                "BitableTaskStore FileStore ProviderResultStore Repository "
                "StagedProviderResult StoredFile"
            ),
        ),
        (
            (
                "from feishu_generation_agent.storage.files import FileStore; "
                "from feishu_generation_agent.integrations import "
                "FeishuClient, FeishuDocumentSource, parse_feishu_url; "
                "print(FileStore.__name__, FeishuClient.__name__, "
                "FeishuDocumentSource.__name__, parse_feishu_url.__name__)"
            ),
            "FileStore FeishuClient FeishuDocumentSource parse_feishu_url",
        ),
        (
            (
                "from feishu_generation_agent.integrations import "
                "FeishuClient, FeishuDocumentSource, parse_feishu_url; "
                "from feishu_generation_agent.storage.files import FileStore; "
                "print(FeishuClient.__name__, FeishuDocumentSource.__name__, "
                "parse_feishu_url.__name__, FileStore.__name__)"
            ),
            "FeishuClient FeishuDocumentSource parse_feishu_url FileStore",
        ),
    ],
)
def test_storage_and_integrations_import_in_clean_process(code, expected):
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == expected


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
@pytest.mark.parametrize(
    ("method_name", "id_name"),
    [("accept_ingress", "dedupe_id"), ("accept_action", "action_id")],
)
async def test_dedupe_rejects_invalid_kind_before_conflict_handling(
    tmp_path, method_name, id_name
):
    store = await BitableTaskStore.open(tmp_path / "agent.sqlite3")
    try:
        method = getattr(store, method_name)
        with pytest.raises(TypeError, match="kind"):
            await method(
                **{
                    id_name: "item-1",
                    "kind": None,
                    "command": {"run_id": "run-1"},
                }
            )
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_dedupe_writes_reject_invalid_identifiers_and_kind(tmp_path):
    store = await BitableTaskStore.open(tmp_path / "agent.sqlite3")
    try:
        with pytest.raises(ValueError, match="dedupe_id"):
            await store.accept_ingress(
                dedupe_id="bad/id",
                kind="message",
                command={},
            )
        with pytest.raises(ValueError, match="action_id"):
            await store.accept_action(
                action_id="bad action",
                kind="approve",
                command={},
            )
        with pytest.raises(ValueError, match="kind"):
            await store.accept_ingress(
                dedupe_id="event-1",
                kind="k" * 65,
                command={},
            )
        with pytest.raises(TypeError, match="kind"):
            await store.accept_action(
                action_id="action-1",
                kind=None,  # type: ignore[arg-type]
                command={},
            )
        with pytest.raises(ValueError, match="dedupe_id"):
            await store.finish_ingress(
                "bad/id",
                status="completed",
                result={},
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
async def test_claim_normalizes_safe_feishu_requirement_source(tmp_path):
    store = await BitableTaskStore.open(tmp_path / "agent.sqlite3")
    try:
        claimed = await store.claim(
            app_token="app",
            table_id="tbl",
            view_id="vew",
            record_id="rec",
            source_url=(
                "https://TENANT.FEISHU.CN./docx/docABC?from=bitable#section"
            ),
            display_text="任务 1",
            claimant_open_id="ou_a",
            run_id="run-1",
            thread_id="thread-1",
            reply_context={},
        )

        assert claimed.source_url == "https://tenant.feishu.cn/docx/docABC"
        assert (await store.get_by_run("run-1")).source_url == claimed.source_url
    finally:
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source_url",
    [
        "http://tenant.feishu.cn/docx/docABC",
        "https://evil.example/docx/docABC",
        "https://tenant.feishu.cn/docx/docABC?access_token=fictional",
        "https://tenant.feishu.cn/wiki/wikiABC?X-Amz-Signature=fictional",
        "https://tenant.feishu.cn/docx/docABC?password=fictional",
        "https://tenant.feishu.cn/docx/docABC?jwt=fictional",
        "https://tenant.feishu.cn/docx/doc%3Faccess_token%3Dfictional",
        "https://tenant.feishu.cn/docx/doc%23tenant_access_token%3Dfictional",
        "https://tenant.feishu.cn/docx/doc%09fictional",
        "https://tenant.feishu.cn:444/docx/docABC",
        "https://user:fictional@tenant.feishu.cn/docx/docABC",
        "x" * 2_049,
    ],
)
async def test_claim_rejects_unsafe_requirement_source(tmp_path, source_url):
    store = await BitableTaskStore.open(tmp_path / "agent.sqlite3")
    try:
        with pytest.raises(ValueError, match="source_url"):
            await store.claim(
                app_token="app",
                table_id="tbl",
                view_id="vew",
                record_id="rec",
                source_url=source_url,
                display_text="任务 1",
                claimant_open_id="ou_a",
                run_id="run-1",
                thread_id="thread-1",
                reply_context={},
            )
        assert await store.get_by_record("app", "tbl", "rec") is None
    finally:
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "display_text",
    [
        "x" * 4_097,
        "Authorization: Bearer fictional-token",
        '{"raw_event_body": {"sender": "ou_1"}}',
        r'{"\u0074enant_access_token":"fictional-token"}',
        _wrap_base64(
            base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"p" * 128).decode()
        ),
        base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"p" * 128).decode(),
        "下载 https://files.example/x?credential=fictional",
    ],
)
async def test_claim_rejects_unsafe_display_text(tmp_path, display_text):
    store = await BitableTaskStore.open(tmp_path / "agent.sqlite3")
    try:
        with pytest.raises(ValueError, match="display_text"):
            await store.claim(
                app_token="app",
                table_id="tbl",
                view_id="vew",
                record_id="rec",
                source_url="https://tenant.feishu.cn/docx/docABC",
                display_text=display_text,
                claimant_open_id="ou_a",
                run_id="run-1",
                thread_id="thread-1",
                reply_context={},
            )
    finally:
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("app_token", ""),
        ("table_id", "bad/table"),
        ("view_id", "bad view"),
        ("record_id", "r" * 257),
        ("run_id", "run/1"),
        ("thread_id", "thread 1"),
        ("claimant_open_id", "ou/1"),
    ],
)
async def test_claim_rejects_invalid_structural_identifiers(
    tmp_path, field_name, invalid_value
):
    store = await BitableTaskStore.open(tmp_path / "agent.sqlite3")
    parameters = {
        "app_token": "app",
        "table_id": "tbl",
        "view_id": "vew",
        "record_id": "rec",
        "source_url": "https://tenant.feishu.cn/docx/docABC",
        "display_text": "任务 1",
        "claimant_open_id": "ou_a",
        "run_id": "run-1",
        "thread_id": "thread-1",
        "reply_context": {},
    }
    parameters[field_name] = invalid_value
    try:
        with pytest.raises(ValueError, match=field_name):
            await store.claim(**parameters)
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
        await store.advance_approval("run-1", PLAN_FINGERPRINT_A)
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
            await store.advance_approval("missing", PLAN_FINGERPRINT_A)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_approval_version_only_advances_for_a_new_fingerprint(tmp_path):
    store = await BitableTaskStore.open(tmp_path / "agent.sqlite3")
    try:
        await _claim(store)

        first, first_changed = await store.advance_approval(
            "run-1", PLAN_FINGERPRINT_A
        )
        replay, replay_changed = await store.advance_approval(
            "run-1", PLAN_FINGERPRINT_A
        )
        changed, plan_changed = await store.advance_approval(
            "run-1", PLAN_FINGERPRINT_B
        )

        assert first.approval_version == 1
        assert first_changed
        assert replay.approval_version == 1
        assert not replay_changed
        assert changed.approval_version == 2
        assert plan_changed
        assert changed.plan_fingerprint == PLAN_FINGERPRINT_B
    finally:
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fingerprint",
    ["", "plan-a", "a" * 63, "A" * 64, "g" * 64, "a" * 65],
)
async def test_approval_rejects_non_sha256_fingerprint(tmp_path, fingerprint):
    store = await BitableTaskStore.open(tmp_path / "agent.sqlite3")
    try:
        await _claim(store)
        with pytest.raises(ValueError, match="plan_fingerprint"):
            await store.advance_approval("run-1", fingerprint)
        binding = await store.get_by_run("run-1")
        assert binding.approval_version == 0
        assert binding.plan_fingerprint is None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_task_updates_reject_invalid_run_id_and_status(tmp_path):
    store = await BitableTaskStore.open(tmp_path / "agent.sqlite3")
    try:
        await _claim(store)
        with pytest.raises(ValueError, match="run_id"):
            await store.set_status("bad/run", TableTaskStatus.FAILED)
        with pytest.raises(ValueError, match="run_id"):
            await store.release("bad run", status=TableTaskStatus.FAILED)
        with pytest.raises(ValueError, match="run_id"):
            await store.advance_approval("bad/run", PLAN_FINGERPRINT_A)
        with pytest.raises(ValueError, match="status"):
            await store.set_status(
                "run-1",
                "failed",  # type: ignore[arg-type]
            )
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_two_connections_emit_one_approval_change_for_same_fingerprint(tmp_path):
    database_path = tmp_path / "agent.sqlite3"
    stores = [
        await BitableTaskStore.open(database_path),
        await BitableTaskStore.open(database_path),
    ]
    await _claim(stores[0])
    start = asyncio.Event()
    ready = 0
    ready_lock = asyncio.Lock()

    async def advance(store):
        nonlocal ready
        async with ready_lock:
            ready += 1
            if ready == len(stores):
                start.set()
        await start.wait()
        return await store.advance_approval("run-1", PLAN_FINGERPRINT_A)

    try:
        results = await asyncio.gather(*(advance(store) for store in stores))

        assert sum(changed for _, changed in results) == 1
        assert {binding.approval_version for binding, _ in results} == {1}
        assert {binding.plan_fingerprint for binding, _ in results} == {
            PLAN_FINGERPRINT_A
        }
    finally:
        await asyncio.gather(*(store.close() for store in stores))


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
        {"apikey": "fictional-key"},
        {"accesskey": "fictional-key"},
        {"privatekey": "fictional-key"},
        {"xprivatekey": "fictional-key"},
        {"privateKey": "fictional-key"},
        {"private-key": "fictional-key"},
        {"privatekey_pem": "fictional-key"},
        {"secretkey": "fictional-key"},
        {"xsecretkey": "fictional-key"},
        {"secretKey": "fictional-key"},
        {"secret-key": "fictional-key"},
        {"secretkey_value": "fictional-key"},
        {"xapikey": "fictional-key"},
        {"xApiKey": "fictional-key"},
        {"x-api-key": "fictional-key"},
        {"xapikey_value": "fictional-key"},
        {"bearer": "fictional-token"},
        {"bearer_value": "fictional-token"},
        {"appsecret": "fictional-secret"},
        {"clientSecret": "fictional-secret"},
        {"access-token": "fictional-token"},
        {"verificationtoken": "fictional-token"},
        {"oauth token": "fictional-token"},
        {"fileToken": "fictional-token"},
        {"app-token": "fictional-token"},
        {"user_access_token": "fictional-token"},
        {"appAccessToken": "fictional-token"},
        {"oauth_token": "fictional-token"},
        {"verification_token": "fictional-token"},
        {"access_key_id": "fictional-access-key"},
        {"jwt": "eyJmaWN0aW9uYWw.payload.signature"},
        {"authorization_header": "Bearer fictional-token"},
        {"raw_event_body": {"sender": "ou_1"}},
        {"event_json": "fictional-event"},
        {"event_data": {"sender": "ou_1"}},
        {"raw": {"sender": "ou_1"}},
        {"event": {"sender": {"open_id": "ou_1"}, "text": "raw"}},
        {"payload": {"action": {"value": {"run_id": "run-1"}}}},
        {"auth": "Bearer fictional-secret"},
        {"callback": "https://files.example/x?X-Amz-Signature=fictional"},
        {"download": "https://files.example/x?credential=fictional"},
        {"media": ["https://files.example/x?access_token=fictional"]},
        {"media": ["https://files.example/x?privatekey_pem=fictional"]},
        {"media": ["https://files.example/x?xapikey_value=fictional"]},
        {"image": "data:image/png;base64,ZmFrZQ=="},
        {"feedback": "Authorization: Bearer fictional-token"},
        {"feedback": "调试值 api-key=fictional-secret"},
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
                "file_token": "file-resource-id",
                "wiki_token": "wiki-resource-id",
                "actor_open_id": "ou_1",
                "selected_task_ids": ["task-1"],
                "feedback": "改成暖色\n保留主体构图",
                "content": "YWJjZA==",
                "secretary_name": "林秘书",
                "tokenizer_model": "cl100k_base",
                "passwordless_mode": "enabled",
                "chat_id": "oc_1",
                "message_id": "om_1",
                "source_url": "https://tenant.feishu.cn/docx/doc?view=compact",
                "metadata_url": (
                    "https://files.example/x?tokenizer_model=cl100k_base&"
                    "secretary_name=lin&passwordless_mode=enabled"
                ),
            },
        )
    finally:
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "encoded",
    [
        base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"p" * 128).decode(),
        base64.b64encode(b"\xff\xd8\xff\xe0" + b"j" * 128).decode(),
        base64.b64encode(b"GIF89a" + b"g" * 128).decode(),
        base64.b64encode(b"RIFF" + b"\x80\x00\x00\x00WEBP" + b"w" * 128).decode(),
        base64.b64encode(bytes(range(256))).decode(),
        base64.urlsafe_b64encode(bytes(range(256))).decode().rstrip("="),
        _wrap_base64(
            base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"p" * 128).decode()
        ),
        _wrap_base64(base64.b64encode(bytes(range(256))).decode()),
        _wrap_base64(
            base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"p" * 128).decode(),
            width=4,
        ),
        _wrap_base64(base64.b64encode(bytes(range(256))).decode(), width=4),
        _split_base64_once(
            base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"p" * 128).decode()
        ),
        _split_base64_once(base64.b64encode(bytes(range(256))).decode()),
        _split_base64_in_half(base64.b64encode(bytes(range(96))).decode()),
        base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"p" * 128)
        .decode()
        .rstrip("="),
    ],
)
@pytest.mark.parametrize("entry", ["command", "reply_context", "result"])
async def test_json_write_entries_reject_bare_image_or_long_base64(
    tmp_path, encoded, entry
):
    store = await BitableTaskStore.open(tmp_path / "agent.sqlite3")
    try:
        with pytest.raises(ValueError, match="sensitive"):
            if entry == "command":
                await store.accept_action(
                    action_id="action-1",
                    kind="approve",
                    command={"content": encoded},
                )
            elif entry == "reply_context":
                await store.claim(
                    app_token="app",
                    table_id="tbl",
                    view_id="vew",
                    record_id="rec",
                    source_url="https://x.feishu.cn/docx/doc",
                    display_text="1",
                    claimant_open_id="ou_a",
                    run_id="run-1",
                    thread_id="thread-1",
                    reply_context={"content": encoded},
                )
            else:
                assert await store.accept_ingress(
                    dedupe_id="event-1",
                    kind="approve",
                    command={"run_id": "run-1"},
                )
                await store.finish_ingress(
                    "event-1", status="completed", result={"content": encoded}
                )
    finally:
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "encoded",
    [BMP_1X1_BASE64, TIFF_II_1X1_BASE64, TIFF_MM_MAGIC_BASE64],
)
@pytest.mark.parametrize(
    "entry",
    ["command", "reply_context", "result", "last_error"],
)
async def test_all_text_write_entries_reject_short_bmp_and_tiff_base64(
    tmp_path, encoded, entry
):
    store = await BitableTaskStore.open(tmp_path / "agent.sqlite3")
    try:
        with pytest.raises(ValueError, match="sensitive"):
            if entry == "command":
                await store.accept_action(
                    action_id="action-1",
                    kind="approve",
                    command={"content": encoded},
                )
            elif entry == "reply_context":
                await store.claim(
                    app_token="app",
                    table_id="tbl",
                    view_id="vew",
                    record_id="rec",
                    source_url="https://x.feishu.cn/docx/doc",
                    display_text="1",
                    claimant_open_id="ou_a",
                    run_id="run-1",
                    thread_id="thread-1",
                    reply_context={"content": encoded},
                )
            elif entry == "result":
                assert await store.accept_ingress(
                    dedupe_id="event-1",
                    kind="approve",
                    command={"run_id": "run-1"},
                )
                await store.finish_ingress(
                    "event-1",
                    status="completed",
                    result={"content": encoded},
                )
            else:
                await _claim(store)
                await store.set_status(
                    "run-1",
                    TableTaskStatus.FAILED,
                    last_error=encoded,
                )
    finally:
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["command", "reply_context", "result"])
@pytest.mark.parametrize(
    "serialized",
    [
        '{"tenant_access_token":"fictional-token"}',
        '{"token": "fictional-token"}',
        '{"raw_event_body": {"sender": "ou_1"}}',
        r'{"\u0074enant_access_token":"fictional-token"}',
        r'{"\u0072aw_event_body":{"sender":"ou_1"}}',
        _deeply_escaped_sensitive_json(),
    ],
)
async def test_json_write_entries_reject_quoted_sensitive_labels(
    tmp_path, entry, serialized
):
    store = await BitableTaskStore.open(tmp_path / "agent.sqlite3")
    try:
        with pytest.raises(ValueError, match="sensitive"):
            if entry == "command":
                await store.accept_action(
                    action_id="action-1",
                    kind="approve",
                    command={"text": serialized},
                )
            elif entry == "reply_context":
                await store.claim(
                    app_token="app",
                    table_id="tbl",
                    view_id="vew",
                    record_id="rec",
                    source_url="https://x.feishu.cn/docx/doc",
                    display_text="1",
                    claimant_open_id="ou_a",
                    run_id="run-1",
                    thread_id="thread-1",
                    reply_context={"text": serialized},
                )
            else:
                assert await store.accept_ingress(
                    dedupe_id="event-1",
                    kind="approve",
                    command={"run_id": "run-1"},
                )
                await store.finish_ingress(
                    "event-1", status="completed", result={"text": serialized}
                )
    finally:
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "serialized",
    [
        _escape_json_text('{"token":"fictional-token"}', 1),
        _escape_json_text('{"tenant_access_token":"fictional-token"}', 2),
        _escape_json_text(r'{"\u0072aw_event_body":{"sender":"ou_1"}}', 4),
        _escape_json_text(
            r'{"url":"https:\/\/files.example\/x?X-Amz-Signature=fictional"}',
            2,
        ),
    ],
)
@pytest.mark.parametrize(
    "entry",
    ["command", "reply_context", "result", "last_error", "display_text"],
)
async def test_all_text_write_entries_reject_repeated_json_string_escaping(
    tmp_path, serialized, entry
):
    store = await BitableTaskStore.open(tmp_path / "agent.sqlite3")
    try:
        with pytest.raises(ValueError, match="sensitive") as raised:
            if entry == "command":
                await store.accept_action(
                    action_id="action-1",
                    kind="approve",
                    command={"text": serialized},
                )
            elif entry == "reply_context":
                await store.claim(
                    app_token="app",
                    table_id="tbl",
                    view_id="vew",
                    record_id="rec",
                    source_url="https://x.feishu.cn/docx/doc",
                    display_text="1",
                    claimant_open_id="ou_a",
                    run_id="run-1",
                    thread_id="thread-1",
                    reply_context={"text": serialized},
                )
            elif entry == "result":
                assert await store.accept_ingress(
                    dedupe_id="event-1",
                    kind="approve",
                    command={"run_id": "run-1"},
                )
                await store.finish_ingress(
                    "event-1",
                    status="completed",
                    result={"text": serialized},
                )
            elif entry == "last_error":
                await _claim(store)
                await store.set_status(
                    "run-1",
                    TableTaskStatus.FAILED,
                    last_error=serialized,
                )
            else:
                await store.claim(
                    app_token="app",
                    table_id="tbl",
                    view_id="vew",
                    record_id="rec",
                    source_url="https://x.feishu.cn/docx/doc",
                    display_text=serialized,
                    claimant_open_id="ou_a",
                    run_id="run-1",
                    thread_id="thread-1",
                    reply_context={},
                )
        assert "fictional-token" not in str(raised.value)
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
async def test_oversized_json_value_is_rejected_before_text_scanners(
    tmp_path, monkeypatch
):
    store = await BitableTaskStore.open(tmp_path / "agent.sqlite3")

    def unexpected_scan(*args, **kwargs):
        raise AssertionError("oversized value reached text scanner")

    monkeypatch.setattr(
        bitable_tasks_module,
        "_validate_safe_text",
        unexpected_scan,
    )
    try:
        with pytest.raises(ValueError, match="too large"):
            await store.accept_ingress(
                dedupe_id="event-large",
                kind="message",
                command={"content": "x" * (64 * 1024)},
            )
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_oversized_json_key_is_rejected_before_key_scanner(
    tmp_path, monkeypatch
):
    store = await BitableTaskStore.open(tmp_path / "agent.sqlite3")

    def unexpected_scan(*args, **kwargs):
        raise AssertionError("oversized key reached key scanner")

    monkeypatch.setattr(
        bitable_tasks_module,
        "_is_sensitive_key",
        unexpected_scan,
    )
    try:
        with pytest.raises(ValueError, match="too large"):
            await store.accept_ingress(
                dedupe_id="event-large",
                kind="message",
                command={"x" * (64 * 1024): "value"},
            )
    finally:
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["command", "last_error", "display_text"])
async def test_invalid_unicode_is_rejected_without_echoing_text(tmp_path, entry):
    store = await BitableTaskStore.open(tmp_path / "agent.sqlite3")
    invalid = "\ud800fictional-secret"
    try:
        with pytest.raises(ValueError) as raised:
            if entry == "command":
                await store.accept_action(
                    action_id="action-1",
                    kind="approve",
                    command={"text": invalid},
                )
            elif entry == "last_error":
                await _claim(store)
                await store.set_status(
                    "run-1",
                    TableTaskStatus.FAILED,
                    last_error=invalid,
                )
            else:
                await store.claim(
                    app_token="app",
                    table_id="tbl",
                    view_id="vew",
                    record_id="rec",
                    source_url="https://x.feishu.cn/docx/doc",
                    display_text=invalid,
                    claimant_open_id="ou_a",
                    run_id="run-1",
                    thread_id="thread-1",
                    reply_context={},
                )
        assert type(raised.value) is ValueError
        assert "fictional-secret" not in str(raised.value)
    finally:
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply_context",
    [
        {"access_token": "fictional-token"},
        {"authorization_header": "Bearer fictional-token"},
        {"raw_event_body": "fictional-event"},
        {"tenant_access_token": "fictional-token"},
        {"content": "data:text/plain;base64,ZmFrZQ=="},
        {"note": "下载 https://files.example/x?X-Amz-Signature=fictional"},
    ],
)
async def test_claim_rejects_sensitive_reply_context(tmp_path, reply_context):
    store = await BitableTaskStore.open(tmp_path / "agent.sqlite3")
    try:
        with pytest.raises(ValueError, match="sensitive") as raised:
            await store.claim(
                app_token="app",
                table_id="tbl",
                view_id="vew",
                record_id="rec",
                source_url="https://x.feishu.cn/docx/doc",
                display_text="1",
                claimant_open_id="ou_a",
                run_id="run-1",
                thread_id="thread-1",
                reply_context=reply_context,
            )
        assert "fictional-token" not in str(raised.value)
        assert "ZmFrZQ" not in str(raised.value)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_claim_rejects_non_string_reply_context_before_transaction(tmp_path):
    store = await BitableTaskStore.open(tmp_path / "agent.sqlite3")
    try:
        with pytest.raises(TypeError, match="string keys and values"):
            await store.claim(
                app_token="app",
                table_id="tbl",
                view_id="vew",
                record_id="rec",
                source_url="https://x.feishu.cn/docx/doc",
                display_text="1",
                claimant_open_id="ou_a",
                run_id="run-1",
                thread_id="thread-1",
                reply_context={"nested": {"message_id": "om_1"}},  # type: ignore[dict-item]
            )
        assert await store.get_by_record("app", "tbl", "rec") is None
    finally:
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        {"access_token": "fictional-token"},
        {"authorization_header": "Bearer fictional-token"},
        {"raw_event_body": {"sender": "ou_1"}},
        {"tenant_access_token": "fictional-token"},
        {"content": "data:image/png;base64,ZmFrZQ=="},
        {"note": "下载 https://files.example/x?credential=fictional"},
    ],
)
async def test_finish_rejects_sensitive_result_and_keeps_pending(tmp_path, result):
    store = await BitableTaskStore.open(tmp_path / "agent.sqlite3")
    try:
        assert await store.accept_ingress(
            dedupe_id="event-1",
            kind="approve",
            command={"run_id": "run-1"},
        )

        with pytest.raises(ValueError, match="sensitive"):
            await store.finish_ingress(
                "event-1", status="completed", result=result
            )
        assert await store.finish_ingress(
            "event-1", status="completed", result={"accepted": True}
        )
    finally:
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("method_name", ["set_status", "release"])
@pytest.mark.parametrize(
    "last_error",
    [
        "Authorization: Bearer fictional-token",
        "data:text/plain;base64,ZmFrZQ==",
        "下载 https://files.example/x?access_token=fictional",
        "api_key=fictional-secret",
        "oauth_token=fictional-token",
        "verification_token: fictional-token",
        "tenant_access_token=fictional-token",
        "access_key_id=fictional-access-key",
        "jwt=eyJmaWN0aW9uYWw.payload.signature",
        '{"tenant_access_token":"fictional-token"}',
        '{"token": "fictional-token"}',
        '{"raw_event_body": {"sender": "ou_1"}}',
        r'{"\u0074enant_access_token":"fictional-token"}',
        r'{"\u0072aw_event_body":{"sender":"ou_1"}}',
        _wrap_base64(
            base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"p" * 128).decode()
        ),
        base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"p" * 128).decode(),
        base64.b64encode(bytes(range(256))).decode(),
    ],
)
async def test_status_writes_reject_sensitive_last_error(
    tmp_path, method_name, last_error
):
    store = await BitableTaskStore.open(tmp_path / "agent.sqlite3")
    try:
        await _claim(store)
        method = getattr(store, method_name)
        with pytest.raises(ValueError, match="sensitive") as raised:
            await method(
                "run-1", status=TableTaskStatus.FAILED, last_error=last_error
            )
        assert "fictional-token" not in str(raised.value)
        assert "ZmFrZQ" not in str(raised.value)
        assert (await store.get_by_run("run-1")).status is TableTaskStatus.PROCESSING
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_json_validator_limits_depth_and_node_count(tmp_path):
    store = await BitableTaskStore.open(tmp_path / "agent.sqlite3")
    too_deep: dict[str, object] = {}
    cursor = too_deep
    for _ in range(20):
        nested: dict[str, object] = {}
        cursor["context"] = nested
        cursor = nested
    too_many_nodes = {"items": list(range(2_000))}
    try:
        with pytest.raises(ValueError, match="too deep"):
            await store.accept_action(
                action_id="action-deep", kind="approve", command=too_deep
            )
        with pytest.raises(ValueError, match="too many nodes"):
            await store.accept_ingress(
                dedupe_id="event-wide", kind="message", command=too_many_nodes
            )
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_reply_result_and_last_error_enforce_size_limits(tmp_path):
    store = await BitableTaskStore.open(tmp_path / "agent.sqlite3")
    oversized = "x" * (64 * 1024)
    try:
        with pytest.raises(ValueError, match="too large"):
            await store.claim(
                app_token="app",
                table_id="tbl",
                view_id="vew",
                record_id="rec",
                source_url="https://x.feishu.cn/docx/doc",
                display_text="1",
                claimant_open_id="ou_a",
                run_id="run-1",
                thread_id="thread-1",
                reply_context={"message_id": oversized},
            )
        await _claim(store)
        assert await store.accept_action(
            action_id="action-1", kind="approve", command={"run_id": "run-1"}
        )
        with pytest.raises(ValueError, match="too large"):
            await store.finish_action(
                "action-1", status="completed", result={"message": oversized}
            )
        with pytest.raises(ValueError, match="too large"):
            await store.set_status(
                "run-1", TableTaskStatus.FAILED, last_error=oversized
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


@pytest.mark.asyncio
async def test_cancel_after_commit_is_queued_returns_durable_success(
    tmp_path, monkeypatch
):
    store = await BitableTaskStore.open(tmp_path / "agent.sqlite3")
    commit_started = asyncio.Event()
    allow_commit = asyncio.Event()
    original_commit = store._connection.commit

    async def blocked_commit():
        commit_started.set()
        await allow_commit.wait()
        await original_commit()

    monkeypatch.setattr(store._connection, "commit", blocked_commit)
    claim_task = asyncio.create_task(_claim(store))
    await commit_started.wait()
    claim_task.cancel()
    await asyncio.sleep(0)
    claim_task.cancel()
    await asyncio.sleep(0)
    allow_commit.set()

    try:
        claimed = await claim_task
        assert claimed.run_id == "run-1"
        assert await store.get_by_run("run-1") == claimed
    finally:
        allow_commit.set()
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["accept", "finish"])
async def test_dedupe_writes_share_durable_commit_semantics(
    tmp_path, monkeypatch, operation
):
    store = await BitableTaskStore.open(tmp_path / "agent.sqlite3")
    if operation == "finish":
        await store.accept_ingress(
            dedupe_id="event-1", kind="approve", command={"run_id": "run-1"}
        )
    commit_started = asyncio.Event()
    allow_commit = asyncio.Event()
    original_commit = store._connection.commit

    async def blocked_commit():
        commit_started.set()
        await allow_commit.wait()
        await original_commit()

    monkeypatch.setattr(store._connection, "commit", blocked_commit)
    if operation == "accept":
        write_task = asyncio.create_task(
            store.accept_action(
                action_id="action-1",
                kind="approve",
                command={"run_id": "run-1"},
            )
        )
    else:
        write_task = asyncio.create_task(
            store.finish_ingress(
                "event-1", status="completed", result={"accepted": True}
            )
        )

    try:
        await asyncio.wait_for(commit_started.wait(), timeout=0.1)
        write_task.cancel()
        await asyncio.sleep(0)
        allow_commit.set()

        assert await write_task
    finally:
        allow_commit.set()
        if not write_task.done():
            await write_task
        await store.close()
