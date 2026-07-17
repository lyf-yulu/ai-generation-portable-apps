import base64
import binascii
import os
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from PIL import Image, UnidentifiedImageError

from feishu_generation_agent.domain.artifact import Artifact, ProviderResult
from feishu_generation_agent.domain.errors import (
    AgentError,
    ErrorCategory,
    ErrorDetail,
)
from feishu_generation_agent.integrations.safe_download import ResultDownloader
from feishu_generation_agent.storage.provider_results import (
    ProviderResultStagingError,
    ProviderResultStore,
)


_IMAGE_FORMATS = {
    "GIF": ("image/gif", "gif"),
    "JPEG": ("image/jpeg", "jpg"),
    "PNG": ("image/png", "png"),
    "WEBP": ("image/webp", "webp"),
}
_CONTENT_TYPE_ALIASES = {
    "image/jpg": "image/jpeg",
    "video/x-m4v": "video/mp4",
}


@dataclass(frozen=True, slots=True)
class StoredFile:
    display_name: str
    local_path: Path
    mime_type: str
    size: int
    sha256: str
    width: int | None = None
    height: int | None = None


@dataclass(frozen=True, slots=True)
class MaterializedProviderResult:
    stored: StoredFile
    provider_url: str | None = None


@dataclass(frozen=True, slots=True)
class _VerifiedMedia:
    mime_type: str
    extension: str
    size: int
    sha256: str
    width: int | None = None
    height: int | None = None


class FileStore:
    def __init__(
        self,
        data_dir: Path,
        outputs_dir: Path,
        *,
        max_bytes: int,
        result_downloader: ResultDownloader | None = None,
        provider_result_store: ProviderResultStore | None = None,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self._data_dir = data_dir
        self._outputs_dir = outputs_dir
        self._max_bytes = max_bytes
        self._result_downloader = result_downloader
        self._provider_result_store = provider_result_store

    def save_input(
        self,
        run_id: str,
        filename: str,
        content: bytes,
    ) -> StoredFile:
        self._validate_segment(run_id)
        directory = self._data_dir / "runs" / run_id / "inputs"
        return self._save_atomic(directory, filename, content, None)

    def save_download(
        self,
        run_id: str,
        task_id: str,
        filename: str,
        content: bytes | Iterable[bytes],
        declared_content_type: str,
    ) -> StoredFile:
        self._validate_segment(run_id)
        self._validate_segment(task_id)
        directory = (
            self._outputs_dir / "runs" / run_id / "tasks" / task_id
        )
        return self._save_atomic(
            directory,
            filename,
            content,
            declared_content_type,
        )

    async def materialize_provider_result(
        self,
        run_id: str,
        task_id: str,
        official_id: str,
        index: int,
        result: ProviderResult,
        *,
        kind: str,
    ) -> MaterializedProviderResult:
        self._validate_segment(run_id)
        self._validate_segment(task_id)
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            raise self._provider_result_error("invalid_result_index")
        if kind not in {"image", "video"}:
            raise self._provider_result_error("invalid_artifact_kind")
        if not result.mime_type.startswith(f"{kind}/"):
            raise self._provider_result_error("kind_mime_mismatch")

        provider_url: str | None = None
        try:
            if result.local_path is not None:
                if self._provider_result_store is None:
                    raise self._configuration_error(
                        "provider_result_store"
                    )
                if result.size is None or result.sha256 is None:
                    raise self._provider_result_error(
                        "missing_staged_integrity"
                    )
                content = self._provider_result_store.read_verified(
                    official_id,
                    local_path=result.local_path,
                    mime_type=result.mime_type,
                    size=result.size,
                    digest=result.sha256,
                )
            elif result.base64_data is not None:
                content = self._decode_base64(result.base64_data)
            elif result.url is not None:
                if self._result_downloader is None:
                    raise self._configuration_error("result_downloader")
                content = await self._result_downloader.download(
                    result.url,
                    expected_mime_type=result.mime_type,
                )
                provider_url = self._redacted_provider_url(result.url)
            else:
                raise self._provider_result_error("missing_result_source")

            stored = self.save_download(
                run_id,
                task_id,
                f"result-{index:03d}",
                content,
                result.mime_type,
            )
        except AgentError:
            raise
        except (ProviderResultStagingError, ValueError, TypeError, OSError):
            raise self._provider_result_error("invalid_provider_result") from None
        return MaterializedProviderResult(
            stored=stored,
            provider_url=provider_url,
        )

    def verify_artifact(self, run_id: str, artifact: Artifact) -> bool:
        try:
            self._validate_segment(run_id)
            self._validate_segment(artifact.task_id)
            if artifact.status != "ready":
                return False
            expected_directory = (
                self._outputs_dir
                / "runs"
                / run_id
                / "tasks"
                / artifact.task_id
            )
            if artifact.local_path.parent != expected_directory:
                return False
            content = self._read_scoped_output(
                ("runs", run_id, "tasks", artifact.task_id),
                artifact.local_path.name,
            )
            if len(content) != artifact.size:
                return False
            if sha256(content).hexdigest() != artifact.sha256:
                return False
            verified = self.validate(content, artifact.mime_type)
            expected_kind = "image" if verified.mime_type.startswith("image/") else "video"
            return (
                artifact.kind == expected_kind
                and artifact.local_path.name
                == f"{verified.sha256}.{verified.extension}"
            )
        except (OSError, ValueError, TypeError):
            return False

    def _read_scoped_output(
        self,
        directory_segments: tuple[str, ...],
        filename: str,
    ) -> bytes:
        self._validate_segment(filename)
        directory_flags = os.O_RDONLY | os.O_DIRECTORY
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        descriptors: list[tuple[int, int | None, str | None]] = []
        root_fd = os.open(self._outputs_dir, directory_flags | nofollow)
        descriptors.append((root_fd, None, None))
        current_fd = root_fd
        try:
            for segment in directory_segments:
                self._validate_segment(segment)
                child_fd = os.open(
                    segment,
                    directory_flags | nofollow,
                    dir_fd=current_fd,
                )
                descriptors.append((child_fd, current_fd, segment))
                current_fd = child_fd
            file_fd = os.open(
                filename,
                os.O_RDONLY | nofollow,
                dir_fd=current_fd,
            )
            try:
                before = os.fstat(file_fd)
                if not stat.S_ISREG(before.st_mode):
                    raise OSError("artifact is not a regular file")
                content = bytearray()
                while len(content) <= self._max_bytes:
                    chunk = os.read(
                        file_fd,
                        min(64 * 1024, self._max_bytes + 1 - len(content)),
                    )
                    if not chunk:
                        break
                    content.extend(chunk)
                after = os.fstat(file_fd)
                current = os.stat(
                    filename,
                    dir_fd=current_fd,
                    follow_symlinks=False,
                )
                if (
                    len(content) > self._max_bytes
                    or (before.st_dev, before.st_ino, before.st_size)
                    != (after.st_dev, after.st_ino, after.st_size)
                    or (after.st_dev, after.st_ino) != (current.st_dev, current.st_ino)
                ):
                    raise OSError("artifact changed during verification")
            finally:
                os.close(file_fd)

            for descriptor, parent_fd, segment in reversed(descriptors):
                descriptor_stat = os.fstat(descriptor)
                if parent_fd is None:
                    current_stat = self._outputs_dir.lstat()
                else:
                    current_stat = os.stat(
                        segment,
                        dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                if (descriptor_stat.st_dev, descriptor_stat.st_ino) != (
                    current_stat.st_dev,
                    current_stat.st_ino,
                ):
                    raise OSError("artifact directory changed during verification")
            return bytes(content)
        finally:
            for descriptor, _, _ in reversed(descriptors):
                os.close(descriptor)

    def _decode_base64(self, encoded: str) -> bytes:
        if not isinstance(encoded, str) or not encoded:
            raise self._provider_result_error("invalid_base64")
        max_encoded = ((self._max_bytes + 2) // 3) * 4 + 4
        if len(encoded) > max_encoded:
            raise self._provider_result_error("result_too_large")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise self._provider_result_error("invalid_base64") from None
        if not content or len(content) > self._max_bytes:
            raise self._provider_result_error("result_too_large")
        return content

    @staticmethod
    def _redacted_provider_url(value: str) -> str:
        try:
            parsed = urlsplit(value)
            hostname = parsed.hostname
            port = parsed.port
        except (TypeError, ValueError):
            raise FileStore._provider_result_error("unsafe_result_url") from None
        if (
            parsed.scheme != "https"
            or not hostname
            or parsed.username is not None
            or parsed.password is not None
            or port not in {None, 443}
        ):
            raise FileStore._provider_result_error("unsafe_result_url")
        host = f"[{hostname}]" if ":" in hostname else hostname
        return urlunsplit(("https", host, parsed.path, "", ""))

    @staticmethod
    def _configuration_error(dependency: str) -> AgentError:
        return AgentError(
            ErrorDetail(
                category=ErrorCategory.CONFIGURATION,
                message="生成结果物化依赖未配置",
                technical_detail=f"operation=materialize; dependency={dependency}",
                retryable=False,
            )
        )

    @staticmethod
    def _provider_result_error(cause: str) -> AgentError:
        return AgentError(
            ErrorDetail(
                category=ErrorCategory.PROVIDER_TERMINAL,
                message="供应商生成结果无效",
                technical_detail=f"operation=materialize; cause={cause}",
                retryable=False,
            )
        )

    def validate(
        self,
        content: bytes,
        declared_content_type: str | None = None,
    ) -> _VerifiedMedia:
        if len(content) > self._max_bytes:
            raise ValueError(
                f"media exceeds configured size limit of {self._max_bytes} bytes"
            )

        mime_type: str
        extension: str
        width: int | None = None
        height: int | None = None
        try:
            with Image.open(BytesIO(content)) as image:
                image_format = image.format
                width, height = image.size
                image.verify()
            if image_format not in _IMAGE_FORMATS:
                raise ValueError("unsupported or invalid media content")
            mime_type, extension = _IMAGE_FORMATS[image_format]
        except (OSError, SyntaxError, UnidentifiedImageError):
            video_type = self._identify_video(content)
            if video_type is None:
                raise ValueError("unsupported or invalid media content") from None
            mime_type, extension = video_type

        self._validate_content_type(mime_type, declared_content_type)

        return _VerifiedMedia(
            mime_type=mime_type,
            extension=extension,
            size=len(content),
            sha256=sha256(content).hexdigest(),
            width=width,
            height=height,
        )

    def _save_atomic(
        self,
        directory: Path,
        display_name: str,
        content: bytes | Iterable[bytes],
        declared_content_type: str | None,
    ) -> StoredFile:
        directory.mkdir(parents=True, exist_ok=True)
        part_path = directory / f".{uuid4().hex}.part"
        try:
            size = 0
            digest = sha256()
            header = bytearray()
            with part_path.open("xb") as output:
                for chunk in self._chunks(content):
                    size += len(chunk)
                    if size > self._max_bytes:
                        raise ValueError(
                            "media exceeds configured size limit of "
                            f"{self._max_bytes} bytes"
                        )
                    digest.update(chunk)
                    if len(header) < 128:
                        header.extend(chunk[: 128 - len(header)])
                    output.write(chunk)

            verified = self._validate_path(
                part_path,
                part_path.stat().st_size,
                digest.hexdigest(),
                bytes(header),
                declared_content_type,
            )
            final_path = directory / (
                f"{verified.sha256}.{verified.extension}"
            )
            if self._existing_file_matches(final_path, verified):
                part_path.unlink()
            else:
                part_path.replace(final_path)
            return StoredFile(
                display_name=display_name,
                local_path=final_path,
                mime_type=verified.mime_type,
                size=verified.size,
                sha256=verified.sha256,
                width=verified.width,
                height=verified.height,
            )
        except BaseException:
            part_path.unlink(missing_ok=True)
            raise

    def _existing_file_matches(
        self,
        path: Path,
        expected: _VerifiedMedia,
    ) -> bool:
        descriptor: int | None = None
        try:
            descriptor = os.open(
                path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_size != expected.size:
                return False
            digest = sha256()
            size = 0
            while size <= self._max_bytes:
                chunk = os.read(
                    descriptor,
                    min(64 * 1024, self._max_bytes + 1 - size),
                )
                if not chunk:
                    break
                size += len(chunk)
                digest.update(chunk)
            after = os.fstat(descriptor)
            current = path.lstat()
            return (
                size == expected.size
                and digest.hexdigest() == expected.sha256
                and (before.st_dev, before.st_ino, before.st_size)
                == (after.st_dev, after.st_ino, after.st_size)
                and (after.st_dev, after.st_ino)
                == (current.st_dev, current.st_ino)
            )
        except OSError:
            return False
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _validate_path(
        self,
        path: Path,
        size: int,
        digest: str,
        header: bytes,
        declared_content_type: str | None,
    ) -> _VerifiedMedia:
        mime_type: str
        extension: str
        width: int | None = None
        height: int | None = None
        try:
            with Image.open(path) as image:
                image_format = image.format
                width, height = image.size
                image.verify()
            if image_format not in _IMAGE_FORMATS:
                raise ValueError("unsupported or invalid media content")
            mime_type, extension = _IMAGE_FORMATS[image_format]
        except (OSError, SyntaxError, UnidentifiedImageError):
            video_type = self._identify_video(header)
            if video_type is None:
                raise ValueError("unsupported or invalid media content") from None
            mime_type, extension = video_type

        self._validate_content_type(mime_type, declared_content_type)
        return _VerifiedMedia(
            mime_type=mime_type,
            extension=extension,
            size=size,
            sha256=digest,
            width=width,
            height=height,
        )

    @staticmethod
    def _validate_content_type(
        mime_type: str, declared_content_type: str | None
    ) -> None:
        if declared_content_type is None:
            return
        declared = declared_content_type.split(";", 1)[0].strip().lower()
        declared = _CONTENT_TYPE_ALIASES.get(declared, declared)
        if declared != mime_type:
            raise ValueError(
                "declared Content-Type does not match verified media content"
            )

    @staticmethod
    def _chunks(content: bytes | Iterable[bytes]) -> Iterable[bytes]:
        if isinstance(content, bytes):
            yield content
            return
        for chunk in content:
            if not isinstance(chunk, bytes):
                raise TypeError("media chunks must be bytes")
            yield chunk

    @staticmethod
    def _identify_video(content: bytes) -> tuple[str, str] | None:
        if len(content) >= 12 and content[4:8] == b"ftyp":
            return "video/mp4", "mp4"
        if content.startswith(b"\x1aE\xdf\xa3") and b"webm" in content[:128].lower():
            return "video/webm", "webm"
        return None

    @staticmethod
    def _validate_segment(value: str) -> None:
        if (
            not value
            or value in {".", ".."}
            or "/" in value
            or "\\" in value
            or "\x00" in value
            or len(value) > 255
        ):
            raise ValueError("invalid path segment")
