import asyncio
import base64
from hashlib import sha256
import json
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from feishu_generation_agent.domain import Artifact, ProviderResult
from feishu_generation_agent.domain.errors import AgentError, ErrorCategory
from feishu_generation_agent.storage.files import FileStore
from feishu_generation_agent.storage.provider_results import ProviderResultStore
from feishu_generation_agent.storage.repository import Repository


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
MP4_FIXTURE = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2"


class _FakeResultDownloader:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.calls: list[tuple[str, str]] = []

    async def download(self, url: str, *, expected_mime_type: str) -> bytes:
        self.calls.append((url, expected_mime_type))
        return self.content


class _ControlledWriteConnection:
    def __init__(self) -> None:
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.first_commit_started = asyncio.Event()
        self.release_first_commit = asyncio.Event()
        self.second_execute_started = asyncio.Event()
        self.commit_count = 0
        self.rollback_count = 0

    async def execute(self, sql: str, parameters: tuple[Any, ...]) -> None:
        self.execute_calls.append((sql, parameters))
        if len(self.execute_calls) == 2:
            self.second_execute_started.set()

    async def commit(self) -> None:
        self.commit_count += 1
        if self.commit_count == 1:
            self.first_commit_started.set()
            await self.release_first_commit.wait()

    async def rollback(self) -> None:
        self.rollback_count += 1


class _CommitFailureConnection:
    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.rollback_count = 0

    async def execute(self, sql: str, parameters: tuple[Any, ...]) -> None:
        return None

    async def commit(self) -> None:
        raise self.error

    async def rollback(self) -> None:
        self.rollback_count += 1


class _CancellableCommitConnection:
    def __init__(self) -> None:
        self.commit_started = asyncio.Event()
        self.rollback_count = 0

    async def execute(self, sql: str, parameters: tuple[Any, ...]) -> None:
        return None

    async def commit(self) -> None:
        self.commit_started.set()
        await asyncio.Event().wait()

    async def rollback(self) -> None:
        self.rollback_count += 1


async def test_repository_serializes_writes_through_commit():
    connection = _ControlledWriteConnection()
    repo = Repository(connection)  # type: ignore[arg-type]

    first_write = asyncio.create_task(
        repo.create_run("run-1", "thread-1", "https://example.test/one")
    )
    await connection.first_commit_started.wait()
    second_write = asyncio.create_task(
        repo.append_event("run-1", "download", "running", "started")
    )

    second_started_while_first_was_uncommitted = False
    try:
        await asyncio.wait_for(
            connection.second_execute_started.wait(), timeout=0.05
        )
        second_started_while_first_was_uncommitted = True
    except TimeoutError:
        pass
    finally:
        connection.release_first_commit.set()
        await asyncio.gather(first_write, second_write)

    assert not second_started_while_first_was_uncommitted
    assert connection.commit_count == 2


async def test_repository_rolls_back_when_commit_fails():
    connection = _CommitFailureConnection(RuntimeError("commit failed"))
    repo = Repository(connection)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="commit failed"):
        await repo.append_event("run-1", "download", "failed", "failed")

    assert connection.rollback_count == 1


async def test_repository_rolls_back_when_write_is_cancelled():
    connection = _CancellableCommitConnection()
    repo = Repository(connection)  # type: ignore[arg-type]
    write = asyncio.create_task(
        repo.append_event("run-1", "download", "running", "started")
    )
    await connection.commit_started.wait()

    write.cancel()
    with pytest.raises(asyncio.CancelledError):
        await write

    assert connection.rollback_count == 1


async def test_operation_is_unique_by_run_task_and_name(tmp_path: Path):
    repo = await Repository.open(tmp_path / "agent.sqlite3")
    await repo.save_operation(
        "run-1", "task-1", "submit", "provider-123", "submitted"
    )
    await repo.save_operation(
        "run-1", "task-1", "submit", "provider-123", "submitted"
    )

    operation = await repo.get_operation("run-1", "task-1", "submit")

    assert operation is not None
    assert operation["provider_id"] == "provider-123"
    assert await repo.count_operations() == 1
    await repo.close()


async def test_operation_upsert_replaces_mutable_fields(tmp_path: Path):
    repo = await Repository.open(tmp_path / "agent.sqlite3")
    await repo.save_operation(
        "run-1",
        "task-1",
        "submit",
        "provider-old",
        "submitted",
        {"attempt": 1},
    )
    await repo.save_operation(
        "run-1",
        "task-1",
        "submit",
        "provider-new",
        "completed",
        {"attempt": 2},
    )

    operation = await repo.get_operation("run-1", "task-1", "submit")

    assert operation is not None
    assert operation["provider_id"] == "provider-new"
    assert operation["status"] == "completed"
    assert operation["payload"] == {"attempt": 2}
    await repo.close()


async def test_run_events_are_ordered_and_summary_is_safe(tmp_path: Path):
    repo = await Repository.open(tmp_path / "agent.sqlite3")
    await repo.create_run("run-1", "thread-1", "https://example.test/doc")
    await repo.append_event(
        "run-1",
        "download",
        "running",
        "Authorization: Bearer very-secret-token "
        "https://example.test/file?token=query-secret&ok=1 "
        + "x" * 700,
    )
    await repo.append_event("run-1", "download", "completed", "done")

    events = await repo.list_events("run-1")

    assert [event["status"] for event in events] == ["running", "completed"]
    summary = events[0]["summary"]
    assert "very-secret-token" not in summary
    assert "query-secret" not in summary
    assert "Bearer [REDACTED]" in summary
    assert "token=[REDACTED]" in summary
    assert len(summary) == 500
    await repo.close()


async def test_artifact_is_saved_as_json(tmp_path: Path):
    db_path = tmp_path / "agent.sqlite3"
    repo = await Repository.open(db_path)
    artifact = Artifact(
        artifact_id="artifact-1",
        task_id="task-1",
        kind="image",
        local_path=tmp_path / "image.png",
        mime_type="image/png",
        size=12,
        sha256="a" * 64,
        status="ready",
    )

    await repo.save_artifact("run-1", artifact)
    await repo.save_artifact("run-1", artifact)

    await repo.close()
    async with aiosqlite.connect(db_path) as db:
        row = await (await db.execute(
            """
            SELECT artifact_json, sha256, task_id
            FROM artifacts
            WHERE artifact_id = ?
            """,
            ("artifact-1",),
        )).fetchone()

    assert row is not None
    assert json.loads(row[0])["local_path"] == str(tmp_path / "image.png")
    assert row[1] == "a" * 64
    assert row[2] == artifact.task_id


async def test_repository_migrates_legacy_operations_without_data_loss(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "legacy.sqlite3"
    async with aiosqlite.connect(db_path) as connection:
        await connection.execute(
            """
            CREATE TABLE operations (
              run_id TEXT NOT NULL,
              task_id TEXT NOT NULL,
              operation TEXT NOT NULL,
              provider_id TEXT,
              status TEXT NOT NULL,
              payload_json TEXT NOT NULL DEFAULT '{}',
              updated_at TEXT NOT NULL,
              PRIMARY KEY (run_id, task_id, operation)
            )
            """
        )
        await connection.execute(
            """
            INSERT INTO operations (
              run_id, task_id, operation, provider_id, status,
              payload_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "run-legacy",
                "task-legacy",
                "submit",
                "official-legacy",
                "completed",
                '{"attempt":1}',
                "2026-07-17T00:00:00+00:00",
            ),
        )
        await connection.execute(
            """
            INSERT INTO operations (
              run_id, task_id, operation, provider_id, status,
              payload_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "run-legacy-submitted",
                "task-legacy-submitted",
                "submit",
                "official-legacy-submitted",
                "submitted",
                '{}',
                "2026-07-17T00:00:00+00:00",
            ),
        )
        await connection.commit()

    repo = await Repository.open(db_path)
    operation = await repo.get_operation(
        "run-legacy", "task-legacy", "submit"
    )
    submitted_operation = await repo.get_operation(
        "run-legacy-submitted", "task-legacy-submitted", "submit"
    )
    await repo.close()

    assert operation is not None
    assert operation["provider_id"] == "official-legacy"
    assert operation["status"] == "completed"
    assert operation["phase"] == "succeeded"
    assert operation["client_submission_id"] is None
    assert operation["official_id"] == "official-legacy"
    assert operation["payload"] == {"attempt": 1}
    assert submitted_operation is not None
    assert submitted_operation["phase"] == "submitted"
    assert submitted_operation["official_id"] == "official-legacy-submitted"
    assert len(submitted_operation["client_submission_id"]) == 32
    assert all(
        character in "0123456789abcdef"
        for character in submitted_operation["client_submission_id"]
    )


async def test_submission_intent_is_atomically_created_once_under_concurrency(
    tmp_path: Path,
) -> None:
    repo = await Repository.open(tmp_path / "agent.sqlite3")
    client_id = "a" * 32

    first, second = await asyncio.gather(
        repo.create_submission_intent_if_absent(
            "run-1", "task-1", "seedance", client_id
        ),
        repo.create_submission_intent_if_absent(
            "run-1", "task-1", "seedance", client_id
        ),
    )

    created = [first[0], second[0]]
    rows = [first[1], second[1]]
    assert sorted(created) == [False, True]
    assert all(row["phase"] == "intent_created" for row in rows)
    assert all(row["client_submission_id"] == client_id for row in rows)
    assert all(row["official_id"] is None for row in rows)
    assert all(row["payload"] == {"provider": "seedance"} for row in rows)
    assert await repo.count_operations() == 1
    await repo.close()


async def test_operation_transition_is_compare_and_set_on_all_identity_fields(
    tmp_path: Path,
) -> None:
    repo = await Repository.open(tmp_path / "agent.sqlite3")
    client_id = "b" * 32
    official_id = "ark/task ?fictional"
    created, _ = await repo.create_submission_intent_if_absent(
        "run-1", "task-1", "seedance", client_id
    )
    assert created is True

    stale = await repo.compare_and_set_operation(
        "run-1",
        "task-1",
        "submit",
        expected_phase="intent_created",
        expected_client_submission_id="c" * 32,
        expected_official_id=None,
        phase="submitted",
        official_id=official_id,
    )
    submitted = await repo.compare_and_set_operation(
        "run-1",
        "task-1",
        "submit",
        expected_phase="intent_created",
        expected_client_submission_id=client_id,
        expected_official_id=None,
        phase="submitted",
        official_id=official_id,
    )
    duplicate = await repo.compare_and_set_operation(
        "run-1",
        "task-1",
        "submit",
        expected_phase="intent_created",
        expected_client_submission_id=client_id,
        expected_official_id=None,
        phase="submitted",
        official_id=official_id,
    )
    timed_out = await repo.compare_and_set_operation(
        "run-1",
        "task-1",
        "submit",
        expected_phase="submitted",
        expected_client_submission_id=client_id,
        expected_official_id=official_id,
        phase="timed_out",
        official_id=official_id,
    )

    assert stale is False
    assert submitted is True
    assert duplicate is False
    assert timed_out is True
    operation = await repo.get_operation("run-1", "task-1", "submit")
    assert operation is not None
    assert operation["phase"] == "timed_out"
    assert operation["client_submission_id"] == client_id
    assert operation["official_id"] == official_id
    assert operation["provider_id"] == official_id
    assert operation["status"] == "timed_out"
    await repo.close()


@pytest.mark.parametrize(
    ("provider", "client_id"),
    [
        ("", "a" * 32),
        ("seedance", "A" * 32),
        ("seedance", "a" * 31),
        ("seedance\nsecret", "a" * 32),
    ],
)
async def test_submission_intent_rejects_unsafe_provider_or_client_id(
    tmp_path: Path,
    provider: str,
    client_id: str,
) -> None:
    repo = await Repository.open(tmp_path / "agent.sqlite3")

    with pytest.raises(ValueError):
        await repo.create_submission_intent_if_absent(
            "run-1", "task-1", provider, client_id
        )

    assert await repo.count_operations() == 0
    await repo.close()


async def test_operation_transition_rejects_unknown_phase_and_unsafe_official_id(
    tmp_path: Path,
) -> None:
    repo = await Repository.open(tmp_path / "agent.sqlite3")
    client_id = "d" * 32
    await repo.create_submission_intent_if_absent(
        "run-1", "task-1", "seedance", client_id
    )

    for phase, official_id in [
        ("completed", "official-id"),
        ("submitted", ""),
        ("submitted", "official\nid"),
        ("submitted", "x" * 513),
    ]:
        with pytest.raises(ValueError):
            await repo.compare_and_set_operation(
                "run-1",
                "task-1",
                "submit",
                expected_phase="intent_created",
                expected_client_submission_id=client_id,
                expected_official_id=None,
                phase=phase,
                official_id=official_id,
            )

    operation = await repo.get_operation("run-1", "task-1", "submit")
    assert operation is not None
    assert operation["phase"] == "intent_created"
    await repo.close()


async def test_artifact_queries_are_idempotent_and_scoped_by_run_and_task(
    tmp_path: Path,
) -> None:
    repo = await Repository.open(tmp_path / "agent.sqlite3")
    artifacts = [
        Artifact(
            artifact_id="artifact-1",
            task_id="task-1",
            kind="image",
            local_path=tmp_path / "one.png",
            mime_type="image/png",
            size=12,
            sha256="a" * 64,
            status="ready",
        ),
        Artifact(
            artifact_id="artifact-2",
            task_id="task-2",
            kind="video",
            local_path=tmp_path / "two.mp4",
            mime_type="video/mp4",
            size=24,
            sha256="b" * 64,
            status="ready",
        ),
    ]
    for artifact in artifacts:
        await repo.save_artifact("run-1", artifact)
    await repo.save_artifact("run-1", artifacts[0])

    one = await repo.get_artifact("artifact-1")
    all_for_run = await repo.list_artifacts("run-1")
    task_two = await repo.list_artifacts("run-1", task_id="task-2")

    assert one == artifacts[0]
    assert all_for_run == artifacts
    assert task_two == [artifacts[1]]
    await repo.close()


def test_same_image_bytes_reuse_hash_path(tmp_path: Path):
    store = FileStore(tmp_path / "data", tmp_path / "outputs", max_bytes=1024)

    first = store.save_input("run-1", "ref.png", PNG_1X1)
    second = store.save_input("run-1", "copy.png", PNG_1X1)

    assert first.sha256 == second.sha256
    assert first.local_path == second.local_path
    assert first.mime_type == "image/png"
    assert first.width == 1
    assert first.height == 1


def test_input_extension_comes_from_verified_content(tmp_path: Path):
    store = FileStore(tmp_path / "data", tmp_path / "outputs", max_bytes=1024)

    stored = store.save_input("run-1", "misleading.jpg", PNG_1X1)

    assert stored.display_name == "misleading.jpg"
    assert stored.local_path.suffix == ".png"
    assert stored.local_path.name == f"{stored.sha256}.png"


def test_invalid_image_is_rejected_without_partial_file(tmp_path: Path):
    store = FileStore(tmp_path / "data", tmp_path / "outputs", max_bytes=1024)

    with pytest.raises(ValueError, match="unsupported or invalid media"):
        store.save_input("run-1", "fake.png", b"not an image")

    assert not list((tmp_path / "data").rglob("*.part"))


def test_size_limit_is_enforced(tmp_path: Path):
    store = FileStore(
        tmp_path / "data", tmp_path / "outputs", max_bytes=len(PNG_1X1) - 1
    )

    with pytest.raises(ValueError, match="size limit"):
        store.save_input("run-1", "ref.png", PNG_1X1)


@pytest.mark.parametrize("unsafe", ["../run", "a/b", "a\\b", ".", "..", ""])
def test_run_and_task_segments_reject_traversal(tmp_path: Path, unsafe: str):
    store = FileStore(tmp_path / "data", tmp_path / "outputs", max_bytes=1024)

    with pytest.raises(ValueError, match="path segment"):
        store.save_input(unsafe, "ref.png", PNG_1X1)
    with pytest.raises(ValueError, match="path segment"):
        store.save_download("run-1", unsafe, "out.png", PNG_1X1, "image/png")


def test_caller_filename_never_controls_output_path(tmp_path: Path):
    store = FileStore(tmp_path / "data", tmp_path / "outputs", max_bytes=1024)

    stored = store.save_download(
        "run-1", "task-1", "../../escape.jpg", PNG_1X1, "image/png"
    )

    assert stored.display_name == "../../escape.jpg"
    assert stored.local_path.parent == (
        tmp_path / "outputs" / "runs" / "run-1" / "tasks" / "task-1"
    )
    assert stored.local_path.name == f"{stored.sha256}.png"


def test_download_rejects_mismatched_content_type_and_cleans_part(tmp_path: Path):
    store = FileStore(tmp_path / "data", tmp_path / "outputs", max_bytes=1024)

    with pytest.raises(ValueError, match="Content-Type"):
        store.save_download(
            "run-1", "task-1", "out.jpg", PNG_1X1, "image/jpeg"
        )

    assert not list((tmp_path / "outputs").rglob("*.part"))


def test_download_accepts_chunks_and_reuses_content_path(tmp_path: Path):
    store = FileStore(tmp_path / "data", tmp_path / "outputs", max_bytes=1024)
    chunks = [PNG_1X1[:20], PNG_1X1[20:]]

    first = store.save_download(
        "run-1", "task-1", "one.png", chunks, "image/png; charset=binary"
    )
    second = store.save_download(
        "run-1", "task-1", "two.png", PNG_1X1, "image/png"
    )

    assert first.local_path == second.local_path
    assert first.size == len(PNG_1X1)
    assert not list((tmp_path / "outputs").rglob("*.part"))


def test_chunked_image_download_does_not_read_entire_part_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store = FileStore(tmp_path / "data", tmp_path / "outputs", max_bytes=1024)
    chunks = [PNG_1X1[:11], PNG_1X1[11:37], PNG_1X1[37:]]

    def reject_read_bytes(path: Path) -> bytes:
        raise AssertionError(f"unexpected whole-file read: {path}")

    monkeypatch.setattr(Path, "read_bytes", reject_read_bytes)

    stored = store.save_download(
        "run-1", "task-1", "streamed.png", chunks, "image/png"
    )

    assert stored.size == len(PNG_1X1)
    assert stored.mime_type == "image/png"
    assert stored.local_path.exists()


@pytest.mark.asyncio
async def test_materialize_local_result_reopens_staging_safely_and_copies_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_store = ProviderResultStore(
        tmp_path / "staging",
        max_item_bytes=1024,
    )
    official_id, staged = provider_store.save(
        [(PNG_1X1, "image/png")],
        provider_task_id="a" * 32,
    )
    store = FileStore(
        tmp_path / "data",
        tmp_path / "outputs",
        max_bytes=1024,
        provider_result_store=provider_store,
    )

    def reject_read_bytes(path: Path) -> bytes:
        raise AssertionError(f"unsafe whole-path read: {path}")

    monkeypatch.setattr(Path, "read_bytes", reject_read_bytes)
    materialized = await store.materialize_provider_result(
        "run-1",
        "task-1",
        official_id,
        0,
        ProviderResult(
            local_path=staged[0].local_path,
            mime_type=staged[0].mime_type,
            size=staged[0].size,
            sha256=staged[0].sha256,
        ),
        kind="image",
    )

    assert materialized.provider_url is None
    assert materialized.stored.local_path.parent == (
        tmp_path / "outputs" / "runs" / "run-1" / "tasks" / "task-1"
    )
    assert materialized.stored.local_path != staged[0].local_path
    assert materialized.stored.mime_type == "image/png"
    assert materialized.stored.sha256 == sha256(PNG_1X1).hexdigest()
    with materialized.stored.local_path.open("rb") as output:
        assert output.read() == PNG_1X1


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["wrong_id", "changed", "symlink"])
async def test_materialize_local_result_rejects_wrong_or_tampered_staging(
    tmp_path: Path,
    tamper: str,
) -> None:
    provider_store = ProviderResultStore(
        tmp_path / "staging",
        max_item_bytes=1024,
    )
    official_id, staged = provider_store.save(
        [(PNG_1X1, "image/png")],
        provider_task_id="b" * 32,
    )
    result = ProviderResult(
        local_path=staged[0].local_path,
        mime_type=staged[0].mime_type,
        size=staged[0].size,
        sha256=staged[0].sha256,
    )
    if tamper == "wrong_id":
        official_id = "c" * 32
    elif tamper == "changed":
        result.local_path.write_bytes(b"tampered")
    else:
        original = result.local_path.with_suffix(".original")
        result.local_path.rename(original)
        result.local_path.symlink_to(original)
    store = FileStore(
        tmp_path / "data",
        tmp_path / "outputs",
        max_bytes=1024,
        provider_result_store=provider_store,
    )

    with pytest.raises(AgentError) as caught:
        await store.materialize_provider_result(
            "run-1",
            "task-1",
            official_id,
            0,
            result,
            kind="image",
        )

    assert caught.value.detail.category == ErrorCategory.PROVIDER_TERMINAL
    assert not list((tmp_path / "outputs").rglob("*.png"))


@pytest.mark.asyncio
async def test_materialize_strict_base64_verifies_magic_and_size(
    tmp_path: Path,
) -> None:
    store = FileStore(
        tmp_path / "data", tmp_path / "outputs", max_bytes=1024
    )
    materialized = await store.materialize_provider_result(
        "run-1",
        "task-1",
        "official-image",
        0,
        ProviderResult(
            base64_data=base64.b64encode(PNG_1X1).decode("ascii"),
            mime_type="image/png",
        ),
        kind="image",
    )

    assert materialized.stored.mime_type == "image/png"
    assert materialized.provider_url is None

    for encoded in (
        "%%%not-base64%%%",
        base64.b64encode(b"not-an-image").decode("ascii"),
        base64.b64encode(b"x" * 1025).decode("ascii"),
    ):
        with pytest.raises(AgentError) as caught:
            await store.materialize_provider_result(
                "run-1",
                "task-1",
                "official-image",
                0,
                ProviderResult(base64_data=encoded, mime_type="image/png"),
                kind="image",
            )
        assert caught.value.detail.category == ErrorCategory.PROVIDER_TERMINAL


@pytest.mark.asyncio
async def test_materialize_url_passes_full_signed_url_only_to_downloader(
    tmp_path: Path,
) -> None:
    marker = "SIGNED_TOKEN_MARKER_NEVER_PERSIST"
    raw_url = f"https://cdn.fictional.test/video.mp4?token={marker}"
    downloader = _FakeResultDownloader(MP4_FIXTURE)
    store = FileStore(
        tmp_path / "data",
        tmp_path / "outputs",
        max_bytes=1024,
        result_downloader=downloader,
    )

    materialized = await store.materialize_provider_result(
        "run-1",
        "task-video",
        "ark-official",
        0,
        ProviderResult(
            url=raw_url,
            url_trust="untrusted",
            mime_type="video/mp4",
        ),
        kind="video",
    )

    assert downloader.calls == [(raw_url, "video/mp4")]
    assert materialized.provider_url == (
        "https://cdn.fictional.test/video.mp4"
    )
    assert marker not in materialized.provider_url
    assert materialized.stored.mime_type == "video/mp4"
    assert materialized.stored.local_path.is_file()


@pytest.mark.asyncio
async def test_materialize_rejects_missing_dependencies_and_fake_video(
    tmp_path: Path,
) -> None:
    store = FileStore(
        tmp_path / "data", tmp_path / "outputs", max_bytes=1024
    )
    local_result = ProviderResult(
        local_path=tmp_path / "staging" / ("d" * 32) / "result-000.png",
        mime_type="image/png",
        size=len(PNG_1X1),
        sha256=sha256(PNG_1X1).hexdigest(),
    )
    url_result = ProviderResult(
        url="https://cdn.fictional.test/result.mp4",
        url_trust="untrusted",
        mime_type="video/mp4",
    )
    for result, kind in ((local_result, "image"), (url_result, "video")):
        with pytest.raises(AgentError) as caught:
            await store.materialize_provider_result(
                "run-1", "task-1", "d" * 32, 0, result, kind=kind
            )
        assert caught.value.detail.category == ErrorCategory.CONFIGURATION

    fake_downloader = _FakeResultDownloader(b"not-really-an-mp4")
    configured = FileStore(
        tmp_path / "data",
        tmp_path / "outputs",
        max_bytes=1024,
        result_downloader=fake_downloader,
    )
    with pytest.raises(AgentError) as caught:
        await configured.materialize_provider_result(
            "run-1", "task-1", "official-video", 0, url_result, kind="video"
        )
    assert caught.value.detail.category == ErrorCategory.PROVIDER_TERMINAL


def test_verify_artifact_accepts_only_intact_task_scoped_media(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FileStore(
        tmp_path / "data", tmp_path / "outputs", max_bytes=1024
    )
    stored = store.save_download(
        "run-1", "task-1", "result", PNG_1X1, "image/png"
    )
    artifact = Artifact(
        artifact_id="artifact-1",
        task_id="task-1",
        kind="image",
        local_path=stored.local_path,
        mime_type=stored.mime_type,
        size=stored.size,
        sha256=stored.sha256,
        status="ready",
    )

    def reject_read_bytes(path: Path) -> bytes:
        raise AssertionError(f"unsafe whole-path read: {path}")

    monkeypatch.setattr(Path, "read_bytes", reject_read_bytes)
    assert store.verify_artifact("run-1", artifact)


@pytest.mark.parametrize("tamper", ["changed", "symlink", "outside"])
def test_verify_artifact_rejects_corrupt_symlink_or_wrong_scope(
    tmp_path: Path,
    tamper: str,
) -> None:
    store = FileStore(
        tmp_path / "data", tmp_path / "outputs", max_bytes=1024
    )
    stored = store.save_download(
        "run-1", "task-1", "result", PNG_1X1, "image/png"
    )
    local_path = stored.local_path
    if tamper == "changed":
        local_path.write_bytes(b"tampered")
    elif tamper == "symlink":
        original = local_path.with_suffix(".original")
        local_path.rename(original)
        local_path.symlink_to(original)
    else:
        outside = tmp_path / "outside.png"
        outside.write_bytes(PNG_1X1)
        local_path = outside
    artifact = Artifact(
        artifact_id="artifact-1",
        task_id="task-1",
        kind="image",
        local_path=local_path,
        mime_type=stored.mime_type,
        size=stored.size,
        sha256=stored.sha256,
        status="ready",
    )

    assert not store.verify_artifact("run-1", artifact)
