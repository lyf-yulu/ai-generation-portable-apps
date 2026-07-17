import json
import os
import re
import shutil
import stat
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any
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
    def __init__(self, staging_dir: Path, *, max_item_bytes: int) -> None:
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

    def save(
        self,
        items: list[tuple[bytes, str]],
    ) -> tuple[str, list[StagedProviderResult]]:
        if not items:
            raise ProviderResultStagingError("empty result set")
        provider_task_id = uuid4().hex
        temporary_dir = self._root / f".{provider_task_id}.part"
        final_dir = self._root / provider_task_id
        try:
            temporary_dir.mkdir(mode=0o700)
            manifest_items: list[dict[str, Any]] = []
            for index, (content, mime_type) in enumerate(items):
                if not isinstance(content, bytes) or not content:
                    raise ProviderResultStagingError("invalid result content")
                if len(content) > self._max_item_bytes:
                    raise ProviderResultStagingError("result exceeds size limit")
                extension = _MIME_EXTENSIONS.get(mime_type)
                if extension is None:
                    raise ProviderResultStagingError("unsupported result mime")
                filename = f"result-{index:03d}.{extension}"
                temporary_path = temporary_dir / f".{filename}.part"
                final_path = temporary_dir / filename
                digest = sha256(content).hexdigest()
                with temporary_path.open("xb") as output:
                    output.write(content)
                    output.flush()
                    os.fsync(output.fileno())
                temporary_path.replace(final_path)
                manifest_items.append(
                    {
                        "filename": filename,
                        "mime_type": mime_type,
                        "size": len(content),
                        "sha256": digest,
                    }
                )

            manifest = {"version": 1, "results": manifest_items}
            manifest_part = temporary_dir / ".manifest.json.part"
            manifest_path = temporary_dir / "manifest.json"
            encoded_manifest = json.dumps(
                manifest,
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("utf-8")
            with manifest_part.open("xb") as output:
                output.write(encoded_manifest)
                output.flush()
                os.fsync(output.fileno())
            manifest_part.replace(manifest_path)
            temporary_dir.replace(final_dir)
            return provider_task_id, self.load(provider_task_id)
        except BaseException:
            shutil.rmtree(temporary_dir, ignore_errors=True)
            raise

    def load(self, provider_task_id: str) -> list[StagedProviderResult]:
        if (
            not isinstance(provider_task_id, str)
            or _PROVIDER_TASK_ID.fullmatch(provider_task_id) is None
        ):
            raise ProviderResultStagingError("invalid provider task id")
        result_dir = self._root / provider_task_id
        try:
            directory_stat = result_dir.lstat()
        except OSError as exc:
            raise ProviderResultStagingError("missing result directory") from exc
        if (
            not stat.S_ISDIR(directory_stat.st_mode)
            or stat.S_ISLNK(directory_stat.st_mode)
        ):
            raise ProviderResultStagingError("invalid result directory")

        manifest_bytes = self._read_regular_file(
            result_dir / "manifest.json",
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
            local_path = result_dir / filename
            content = self._read_regular_file(local_path, max_bytes=size)
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
    def _read_regular_file(path: Path, *, max_bytes: int) -> bytes:
        flags = os.O_RDONLY
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
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
