import base64
import json
from pathlib import Path

import aiosqlite
import pytest

from feishu_generation_agent.domain import Artifact
from feishu_generation_agent.storage.files import FileStore
from feishu_generation_agent.storage.repository import Repository


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


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
