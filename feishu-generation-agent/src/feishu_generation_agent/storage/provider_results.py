import json
import os
import re
import shutil
import stat
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4


_PROVIDER_TASK_ID = re.compile(r"[0-9a-f]{32}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MIME_EXTENSIONS = {
    "image/gif": "gif",
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}
_READ_CHUNK_BYTES = 64 * 1024
_MAX_MANIFEST_BYTES = 1024 * 1024


class ProviderResultStagingError(ValueError):
    """A staged synchronous result is missing, malformed, or tampered with."""


@dataclass(frozen=True, slots=True)
class StagedProviderResult:
    local_path: Path
    mime_type: str
    size: int
    sha256: str


class ProviderResultStore:
    def __init__(
        self,
        staging_dir: Path,
        *,
        max_item_bytes: int,
        directory_hook: Callable[[str], None] | None = None,
    ) -> None:
        if not isinstance(staging_dir, Path):
            raise ValueError("staging_dir must be a Path")
        if (
            not isinstance(max_item_bytes, int)
            or isinstance(max_item_bytes, bool)
            or max_item_bytes <= 0
        ):
            raise ValueError("max_item_bytes must be positive")
        staging_dir.mkdir(parents=True, exist_ok=True)
        try:
            root_stat = staging_dir.lstat()
        except OSError as exc:
            raise ValueError("staging_dir is unavailable") from exc
        if not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode):
            raise ValueError("staging_dir must be a directory")
        self._root = staging_dir
        self._max_item_bytes = max_item_bytes
        self._directory_hook = directory_hook

    def save(
        self,
        items: list[tuple[bytes, str]],
        *,
        provider_task_id: str | None = None,
    ) -> tuple[str, list[StagedProviderResult]]:
        if not items:
            raise ProviderResultStagingError("empty result set")
        provider_task_id = (
            uuid4().hex if provider_task_id is None else provider_task_id
        )
        if not self.is_valid_provider_task_id(provider_task_id):
            raise ProviderResultStagingError("invalid provider task id")
        temporary_name = f".{provider_task_id}.part"
        temporary_created = False
        final_created = False
        root_descriptor = self._open_root()
        try:
            root_identity = self._directory_identity(
                os.fstat(root_descriptor)
            )
            self._run_directory_hook("root_opened")
            os.mkdir(temporary_name, mode=0o700, dir_fd=root_descriptor)
            temporary_created = True
            temporary_descriptor = os.open(
                temporary_name,
                self._directory_flags(),
                dir_fd=root_descriptor,
            )
            temporary_identity = self._directory_identity(
                os.fstat(temporary_descriptor)
            )
            manifest_items: list[dict[str, Any]] = []
            try:
                for index, (content, mime_type) in enumerate(items):
                    if not isinstance(content, bytes) or not content:
                        raise ProviderResultStagingError(
                            "invalid result content"
                        )
                    if len(content) > self._max_item_bytes:
                        raise ProviderResultStagingError(
                            "result exceeds size limit"
                        )
                    extension = _MIME_EXTENSIONS.get(mime_type)
                    if extension is None:
                        raise ProviderResultStagingError(
                            "unsupported result mime"
                        )
                    filename = f"result-{index:03d}.{extension}"
                    digest = sha256(content).hexdigest()
                    self._write_atomic(
                        temporary_descriptor,
                        filename,
                        content,
                    )
                    manifest_items.append(
                        {
                            "filename": filename,
                            "mime_type": mime_type,
                            "size": len(content),
                            "sha256": digest,
                        }
                    )

                manifest = {"version": 1, "results": manifest_items}
                encoded_manifest = json.dumps(
                    manifest,
                    ensure_ascii=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                self._write_atomic(
                    temporary_descriptor,
                    "manifest.json",
                    encoded_manifest,
                )
                self._assert_directory_entry(
                    root_descriptor,
                    temporary_name,
                    temporary_identity,
                )
            finally:
                os.close(temporary_descriptor)
            self._assert_root_current(root_identity)
            os.rename(
                temporary_name,
                provider_task_id,
                src_dir_fd=root_descriptor,
                dst_dir_fd=root_descriptor,
            )
            final_created = True
            self._assert_root_current(root_identity)
        except BaseException:
            if temporary_created:
                self._remove_tree(root_descriptor, temporary_name)
            if final_created:
                self._remove_tree(root_descriptor, provider_task_id)
            raise
        finally:
            os.close(root_descriptor)
        return provider_task_id, self.load(provider_task_id)

    def load(self, provider_task_id: str) -> list[StagedProviderResult]:
        if not self.is_valid_provider_task_id(provider_task_id):
            raise ProviderResultStagingError("invalid provider task id")
        root_descriptor = self._open_root()
        try:
            root_identity = self._directory_identity(os.fstat(root_descriptor))
            self._run_directory_hook("root_opened")
            try:
                result_descriptor = os.open(
                    provider_task_id,
                    self._directory_flags(),
                    dir_fd=root_descriptor,
                )
            except OSError as exc:
                raise ProviderResultStagingError(
                    "missing result directory"
                ) from exc
            try:
                result_identity = self._directory_identity(
                    os.fstat(result_descriptor)
                )
                self._run_directory_hook("result_opened")
                staged = self._load_from_directory(
                    result_descriptor,
                    provider_task_id,
                )
                self._assert_directory_entry(
                    root_descriptor,
                    provider_task_id,
                    result_identity,
                )
                self._assert_root_current(root_identity)
                return staged
            finally:
                os.close(result_descriptor)
        finally:
            os.close(root_descriptor)

    def _load_from_directory(
        self,
        result_descriptor: int,
        provider_task_id: str,
    ) -> list[StagedProviderResult]:

        manifest_bytes = self._read_regular_file(
            "manifest.json",
            dir_fd=result_descriptor,
            max_bytes=_MAX_MANIFEST_BYTES,
        )
        try:
            manifest = json.loads(manifest_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ProviderResultStagingError("invalid manifest") from None
        if (
            not isinstance(manifest, dict)
            or set(manifest) != {"version", "results"}
            or manifest.get("version") != 1
            or not isinstance(manifest.get("results"), list)
            or not manifest["results"]
        ):
            raise ProviderResultStagingError("invalid manifest")

        staged: list[StagedProviderResult] = []
        for index, item in enumerate(manifest["results"]):
            if not isinstance(item, dict) or set(item) != {
                "filename",
                "mime_type",
                "size",
                "sha256",
            }:
                raise ProviderResultStagingError("invalid manifest item")
            mime_type = item["mime_type"]
            extension = _MIME_EXTENSIONS.get(mime_type)
            filename = item["filename"]
            size = item["size"]
            digest = item["sha256"]
            if (
                extension is None
                or filename != f"result-{index:03d}.{extension}"
                or not isinstance(size, int)
                or isinstance(size, bool)
                or size <= 0
                or size > self._max_item_bytes
                or not isinstance(digest, str)
                or _SHA256.fullmatch(digest) is None
            ):
                raise ProviderResultStagingError("invalid manifest item")
            local_path = self._root / provider_task_id / filename
            content = self._read_regular_file(
                filename,
                dir_fd=result_descriptor,
                max_bytes=size,
            )
            if len(content) != size or sha256(content).hexdigest() != digest:
                raise ProviderResultStagingError("result integrity mismatch")
            staged.append(
                StagedProviderResult(
                    local_path=local_path,
                    mime_type=mime_type,
                    size=size,
                    sha256=digest,
                )
            )
        return staged

    @staticmethod
    def is_valid_provider_task_id(value: object) -> bool:
        return (
            isinstance(value, str)
            and _PROVIDER_TASK_ID.fullmatch(value) is not None
        )

    @staticmethod
    def _read_regular_file(
        filename: str,
        *,
        dir_fd: int,
        max_bytes: int,
    ) -> bytes:
        flags = os.O_RDONLY
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(filename, flags, dir_fd=dir_fd)
        except OSError as exc:
            raise ProviderResultStagingError("staged file unavailable") from exc
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
                raise ProviderResultStagingError("invalid staged file")
            content = bytearray()
            while True:
                chunk = os.read(descriptor, _READ_CHUNK_BYTES)
                if not chunk:
                    break
                if len(content) + len(chunk) > max_bytes:
                    raise ProviderResultStagingError("staged file exceeds size limit")
                content.extend(chunk)
            after = os.fstat(descriptor)
            if (
                before.st_dev,
                before.st_ino,
                before.st_mode,
                before.st_size,
                before.st_mtime_ns,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_mode,
                after.st_size,
                after.st_mtime_ns,
            ):
                raise ProviderResultStagingError("staged file changed while reading")
            return bytes(content)
        finally:
            os.close(descriptor)

    @staticmethod
    def _directory_flags() -> int:
        return (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )

    def _open_root(self) -> int:
        try:
            descriptor = os.open(self._root, self._directory_flags())
        except OSError as exc:
            raise ProviderResultStagingError("staging root unavailable") from exc
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise ProviderResultStagingError("invalid staging root")
        return descriptor

    @staticmethod
    def _directory_identity(value: os.stat_result) -> tuple[int, int, int]:
        return value.st_dev, value.st_ino, value.st_mode

    def _assert_root_current(
        self,
        expected_identity: tuple[int, int, int],
    ) -> None:
        try:
            current = self._root.lstat()
        except OSError as exc:
            raise ProviderResultStagingError("staging root changed") from exc
        if (
            stat.S_ISLNK(current.st_mode)
            or self._directory_identity(current) != expected_identity
        ):
            raise ProviderResultStagingError("staging root changed")

    @staticmethod
    def _assert_directory_entry(
        parent_descriptor: int,
        name: str,
        expected_identity: tuple[int, int, int],
    ) -> None:
        try:
            current = os.stat(
                name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise ProviderResultStagingError("result directory changed") from exc
        if (
            stat.S_ISLNK(current.st_mode)
            or ProviderResultStore._directory_identity(current)
            != expected_identity
        ):
            raise ProviderResultStagingError("result directory changed")

    @staticmethod
    def _write_atomic(
        directory_descriptor: int,
        filename: str,
        content: bytes,
    ) -> None:
        temporary_name = f".{filename}.part"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(
            temporary_name,
            flags,
            0o600,
            dir_fd=directory_descriptor,
        )
        try:
            view = memoryview(content)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.rename(
            temporary_name,
            filename,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
        )

    def _run_directory_hook(self, event: str) -> None:
        if self._directory_hook is not None:
            self._directory_hook(event)

    @staticmethod
    def _remove_tree(root_descriptor: int, name: str) -> None:
        try:
            shutil.rmtree(name, dir_fd=root_descriptor)
        except (FileNotFoundError, NotADirectoryError):
            pass
