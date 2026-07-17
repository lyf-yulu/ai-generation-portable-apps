from pathlib import Path

import pytest

from feishu_generation_agent.storage.provider_results import (
    ProviderResultStagingError,
    ProviderResultStore,
)


@pytest.mark.parametrize("target", ["root", "result"])
def test_load_rejects_directory_replacement_after_directory_fd_open(
    tmp_path: Path,
    target: str,
) -> None:
    staging_dir = tmp_path / "staging"
    armed = False
    provider_task_id = ""

    def replace_directory(event: str) -> None:
        if not armed or event != f"{target}_opened":
            return
        if target == "root":
            original = tmp_path / "original-staging"
            outside = tmp_path / "outside-root"
            outside.mkdir()
            staging_dir.rename(original)
            staging_dir.symlink_to(outside, target_is_directory=True)
        else:
            result_dir = staging_dir / provider_task_id
            original = staging_dir / f"{provider_task_id}.original"
            outside = tmp_path / "outside-result"
            outside.mkdir()
            result_dir.rename(original)
            result_dir.symlink_to(outside, target_is_directory=True)

    store = ProviderResultStore(
        staging_dir,
        max_item_bytes=1024,
        directory_hook=replace_directory,
    )
    provider_task_id, _ = store.save(
        [(b"trusted-result", "image/png")],
    )
    armed = True

    with pytest.raises(ProviderResultStagingError):
        store.load(provider_task_id)


def test_save_rejects_root_replacement_without_writing_outside(
    tmp_path: Path,
) -> None:
    staging_dir = tmp_path / "staging"
    outside = tmp_path / "outside"
    outside.mkdir()
    armed = False

    def replace_root(event: str) -> None:
        nonlocal armed
        if not armed or event != "root_opened":
            return
        armed = False
        staging_dir.rename(tmp_path / "original-staging")
        staging_dir.symlink_to(outside, target_is_directory=True)

    store = ProviderResultStore(
        staging_dir,
        max_item_bytes=1024,
        directory_hook=replace_root,
    )
    armed = True

    with pytest.raises(ProviderResultStagingError):
        store.save([(b"trusted-result", "image/png")])

    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("provider_task_id", ["", "A" * 32, "a" * 31])
def test_save_rejects_explicit_invalid_provider_task_id(
    tmp_path: Path,
    provider_task_id: str,
) -> None:
    store = ProviderResultStore(
        tmp_path / "staging",
        max_item_bytes=1024,
    )

    with pytest.raises(ProviderResultStagingError):
        store.save(
            [(b"trusted-result", "image/png")],
            provider_task_id=provider_task_id,
        )
