from collections.abc import Iterable
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from PIL import Image, UnidentifiedImageError


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
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self._data_dir = data_dir
        self._outputs_dir = outputs_dir
        self._max_bytes = max_bytes

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
            if final_path.exists():
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
