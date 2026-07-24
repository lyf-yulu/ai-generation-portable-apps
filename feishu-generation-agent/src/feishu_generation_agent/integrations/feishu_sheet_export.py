from __future__ import annotations

import asyncio
import hashlib
from io import BytesIO
import posixpath
from dataclasses import dataclass
from time import monotonic
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile
import zlib

from feishu_generation_agent.domain.errors import (
    AgentError,
    ErrorCategory,
    ErrorDetail,
)
from feishu_generation_agent.integrations.feishu_client import FeishuClient


MAX_BLOCK_TOKEN_LENGTH = 256
MAX_XLSX_COMPRESSED_BYTES = 64 * 1024 * 1024
MAX_XLSX_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
MAX_XLSX_ENTRY_COUNT = 4_096
MAX_XLSX_MEDIA_COUNT = 256
MAX_XLSX_MEDIA_BYTES = 32 * 1024 * 1024
MAX_XLSX_ANCHOR_COUNT = 10_000
MAX_XML_DEPTH = 64
MAX_XML_NODES = 100_000
MAX_XML_BYTES = 8 * 1024 * 1024
EXPORT_POLL_INTERVAL_SECONDS = 0.5
EXPORT_TIMEOUT_SECONDS = 60.0

_DOCUMENT_REL = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
)
_PACKAGE_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_SPREADSHEET = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_DRAWING = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
_DRAWING_MAIN = "http://schemas.openxmlformats.org/drawingml/2006/main"
_REL_ID = f"{{{_DOCUMENT_REL}}}id"
_REL_EMBED = f"{{{_DOCUMENT_REL}}}embed"


@dataclass(frozen=True, slots=True)
class EmbeddedSheetRef:
    spreadsheet_token: str
    sheet_id: str


@dataclass(frozen=True, slots=True)
class SheetImageAnchor:
    row: int
    column: int
    media_name: str
    sha256: str
    worksheet_name: str
    source_sheet_id: str


@dataclass(frozen=True, slots=True)
class ExtractedSheetImage:
    media_name: str
    content: bytes
    sha256: str
    anchors: tuple[SheetImageAnchor, ...]


@dataclass(frozen=True, slots=True)
class ExtractedSheet:
    text_lines: tuple[str, ...]
    images: tuple[ExtractedSheetImage, ...]


@dataclass(slots=True)
class _ImageRecord:
    media_name: str
    content: bytes
    anchors: list[SheetImageAnchor]


@dataclass(slots=True)
class _ImageExtractionState:
    images_by_hash: dict[str, _ImageRecord]
    media_by_path: dict[str, tuple[bytes, str]]
    drawing_paths: set[str]
    anchor_count: int


def parse_sheet_block_token(raw: str) -> EmbeddedSheetRef:
    if (
        not isinstance(raw, str)
        or len(raw) > MAX_BLOCK_TOKEN_LENGTH
        or "/" in raw
        or "\\" in raw
        or "_" not in raw
    ):
        raise ValueError("invalid sheet block token")
    spreadsheet_token, sheet_id = raw.rsplit("_", 1)
    if (
        not _is_safe_token_component(spreadsheet_token)
        or not _is_safe_token_component(sheet_id)
    ):
        raise ValueError("invalid sheet block token")
    return EmbeddedSheetRef(
        spreadsheet_token=spreadsheet_token,
        sheet_id=sheet_id,
    )


class FeishuSheetExporter:
    def __init__(self, client: FeishuClient) -> None:
        self._client = client

    async def export(self, ref: EmbeddedSheetRef) -> ExtractedSheet:
        if not isinstance(ref, EmbeddedSheetRef):
            raise TypeError("ref must be an EmbeddedSheetRef")
        _validate_api_token(ref.spreadsheet_token, "spreadsheet token")
        _validate_source_sheet_id(ref.sheet_id)

        create_payload = await self._client.request_json(
            "POST",
            "/open-apis/drive/v1/export_tasks",
            json_body={
                "file_extension": "xlsx",
                "token": ref.spreadsheet_token,
                "type": "sheet",
            },
        )
        ticket = _response_token(create_payload, "ticket")
        deadline = monotonic() + EXPORT_TIMEOUT_SECONDS
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise _export_timeout()
            try:
                async with asyncio.timeout(remaining):
                    status_payload = await self._client.request_json(
                        "GET",
                        f"/open-apis/drive/v1/export_tasks/{ticket}",
                        params={"token": ref.spreadsheet_token},
                    )
            except TimeoutError:
                raise _export_timeout() from None
            result = status_payload.get("data", {}).get("result")
            if not isinstance(result, dict):
                raise _document_error(
                    "飞书电子表格导出状态响应无效",
                    "export status result is missing",
                )
            job_status = result.get("job_status")
            if not isinstance(job_status, int) or isinstance(job_status, bool):
                raise _document_error(
                    "飞书电子表格导出状态响应无效",
                    "export job status is invalid",
                )
            if job_status == 0:
                file_token = _response_token(
                    {"data": result},
                    "file_token",
                )
                content = await self._client.download_export_file(
                    file_token,
                    max_bytes=MAX_XLSX_COMPRESSED_BYTES,
                )
                return extract_sheet_xlsx(
                    content,
                    source_sheet_id=ref.sheet_id,
                )
            if job_status not in {1, 2}:
                raise _document_error(
                    "飞书电子表格导出失败",
                    "export job entered failure status",
                )
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise _export_timeout()
            await asyncio.sleep(min(EXPORT_POLL_INTERVAL_SECONDS, remaining))


def extract_sheet_xlsx(
    content: bytes,
    *,
    source_sheet_id: str,
) -> ExtractedSheet:
    _validate_source_sheet_id(source_sheet_id)
    if not isinstance(content, bytes):
        raise TypeError("xlsx content must be bytes")
    if len(content) > MAX_XLSX_COMPRESSED_BYTES:
        raise _document_error(
            "飞书电子表格导出文件过大",
            "xlsx compressed bytes limit exceeded",
        )
    try:
        with ZipFile(BytesIO(content)) as archive:
            _validate_archive(archive)
            root_relationships = _relationships(archive, "")
            workbook_path = _relationship_target(
                root_relationships,
                relationship_type="officeDocument",
            )
            workbook = _parse_xml(_read_member(archive, workbook_path, MAX_XML_BYTES))
            _require_xml_root(
                workbook,
                f"{{{_SPREADSHEET}}}workbook",
                "workbook",
            )
            workbook_relationships = _relationships(archive, workbook_path)
            shared_strings = _shared_strings(archive, workbook_relationships)
            text_lines: list[str] = []
            image_state = _ImageExtractionState(
                images_by_hash={},
                media_by_path={},
                drawing_paths=set(),
                anchor_count=0,
            )
            worksheet_count = 0
            worksheet_paths: set[str] = set()

            sheets = workbook.find(f"{{{_SPREADSHEET}}}sheets")
            for sheet in () if sheets is None else tuple(sheets):
                if sheet.tag != f"{{{_SPREADSHEET}}}sheet":
                    continue
                relationship_id = sheet.get(_REL_ID)
                worksheet_name = sheet.get("name")
                if not relationship_id or not worksheet_name:
                    raise _document_error(
                        "飞书电子表格工作表元数据无效",
                        "worksheet metadata is invalid",
                    )
                relationship = workbook_relationships.get(relationship_id)
                if relationship is None:
                    raise _document_error(
                        "飞书电子表格关系文件不完整",
                        "xlsx relationship missing: type=worksheet",
                    )
                if relationship[0] != "worksheet":
                    continue
                worksheet_path = relationship[1]
                if worksheet_path in worksheet_paths:
                    raise _document_error(
                        "飞书电子表格工作表关系重复",
                        "duplicate worksheet target rejected",
                    )
                worksheet_paths.add(worksheet_path)
                worksheet = _parse_xml(
                    _read_member(archive, worksheet_path, MAX_XML_BYTES)
                )
                _require_xml_root(
                    worksheet,
                    f"{{{_SPREADSHEET}}}worksheet",
                    "worksheet",
                )
                worksheet_count += 1
                text_lines.extend(
                    _worksheet_text(
                        worksheet,
                        worksheet_name=worksheet_name,
                        source_sheet_id=source_sheet_id,
                        shared_strings=shared_strings,
                    )
                )
                _worksheet_images(
                    archive,
                    worksheet,
                    worksheet_path=worksheet_path,
                    worksheet_name=worksheet_name,
                    source_sheet_id=source_sheet_id,
                    state=image_state,
                )
            if worksheet_count == 0:
                raise _document_error(
                    "飞书电子表格没有可读取的工作表",
                    "xlsx workbook has no valid worksheets",
                )
    except AgentError:
        raise
    except (BadZipFile, KeyError, ElementTree.ParseError, ValueError) as exc:
        raise _document_error(
            "飞书电子表格导出文件无效或不受支持",
            f"xlsx parse failed: {type(exc).__name__}",
        ) from exc

    images = tuple(
        ExtractedSheetImage(
            media_name=record.media_name,
            content=record.content,
            sha256=sha256,
            anchors=tuple(record.anchors),
        )
        for sha256, record in image_state.images_by_hash.items()
    )
    return ExtractedSheet(text_lines=tuple(text_lines), images=images)


def _relationships(
    archive: ZipFile,
    owner_path: str,
) -> dict[str, tuple[str, str]]:
    if owner_path:
        relationship_path = posixpath.join(
            posixpath.dirname(owner_path),
            "_rels",
            f"{posixpath.basename(owner_path)}.rels",
        )
    else:
        relationship_path = "_rels/.rels"
    root = _parse_xml(_read_member(archive, relationship_path, MAX_XML_BYTES))
    relationships: dict[str, tuple[str, str]] = {}
    seen_relationship_ids: set[str] = set()
    for item in root.findall(f"{{{_PACKAGE_REL}}}Relationship"):
        relationship_id = item.get("Id")
        target = item.get("Target")
        relationship_type = item.get("Type")
        target_mode = item.get("TargetMode")
        if relationship_id:
            if relationship_id in seen_relationship_ids:
                raise _document_error(
                    "飞书电子表格关系文件无效",
                    "duplicate relationship id",
                )
            seen_relationship_ids.add(relationship_id)
        if relationship_id and target and relationship_type:
            if target_mode is not None and target_mode.lower() != "internal":
                if relationship_type.rsplit("/", 1)[-1] in {
                    "officeDocument",
                    "worksheet",
                    "sharedStrings",
                    "drawing",
                    "image",
                }:
                    raise _document_error(
                        "飞书电子表格包含不支持的外部关系",
                        "external relationship target rejected",
                    )
                continue
            relationships[relationship_id] = (
                relationship_type.rsplit("/", 1)[-1],
                _resolve_target(owner_path, target),
            )
    return relationships


def _resolve_target(owner_path: str, target: str) -> str:
    if (
        not target
        or "\\" in target
        or "\x00" in target
        or "?" in target
        or "#" in target
        or posixpath.isabs(target)
        or ":" in target.split("/", 1)[0]
    ):
        raise _document_error(
            "飞书电子表格关系目标不安全",
            "unsafe relationship target",
        )
    resolved = posixpath.normpath(
        posixpath.join(posixpath.dirname(owner_path), target)
    )
    if (
        resolved in {"", ".", ".."}
        or resolved.startswith("../")
        or posixpath.isabs(resolved)
    ):
        raise _document_error(
            "飞书电子表格关系目标不安全",
            "unsafe relationship target",
        )
    return resolved


def _relationship_target(
    relationships: dict[str, tuple[str, str]],
    *,
    relationship_id: str | None = None,
    relationship_type: str,
) -> str:
    if relationship_id is not None:
        candidate = relationships.get(relationship_id)
        if candidate is not None and candidate[0] == relationship_type:
            return candidate[1]
    else:
        for current_type, target in relationships.values():
            if current_type == relationship_type:
                return target
    raise _document_error(
        "飞书电子表格关系文件不完整",
        f"xlsx relationship missing: type={relationship_type}",
    )


def _shared_strings(
    archive: ZipFile,
    relationships: dict[str, tuple[str, str]],
) -> tuple[str, ...]:
    try:
        path = _relationship_target(
            relationships,
            relationship_type="sharedStrings",
        )
    except AgentError:
        return ()
    root = _parse_xml(_read_member(archive, path, MAX_XML_BYTES))
    _require_xml_root(root, f"{{{_SPREADSHEET}}}sst", "shared strings")
    return tuple(
        "".join(text.text or "" for text in item.iter(f"{{{_SPREADSHEET}}}t"))
        for item in root.findall(f"{{{_SPREADSHEET}}}si")
    )


def _worksheet_text(
    worksheet: ElementTree.Element,
    *,
    worksheet_name: str,
    source_sheet_id: str,
    shared_strings: tuple[str, ...],
) -> list[str]:
    values: list[tuple[tuple[int, int], str, str]] = []
    for cell in worksheet.iter(f"{{{_SPREADSHEET}}}c"):
        reference = cell.get("r", "")
        cell_type = cell.get("t")
        value_node = cell.find(f"{{{_SPREADSHEET}}}v")
        value = value_node.text if value_node is not None else None
        if cell_type == "s" and value is not None:
            try:
                shared_string_index = int(value)
            except ValueError as exc:
                raise _document_error(
                    "飞书电子表格共享字符串索引无效",
                    "shared string index is invalid",
                ) from exc
            if not 0 <= shared_string_index < len(shared_strings):
                raise _document_error(
                    "飞书电子表格共享字符串索引无效",
                    "shared string index is invalid",
                )
            value = shared_strings[shared_string_index]
        elif cell_type == "inlineStr":
            value = "".join(
                item.text or ""
                for item in cell.iter(f"{{{_SPREADSHEET}}}t")
            )
        if value:
            values.append((_cell_position(reference), reference, value))
    values.sort(key=lambda item: item[0])
    return [
        f"[sheet:{source_sheet_id} worksheet:{worksheet_name} cell:{reference}] {value}"
        for _, reference, value in values
    ]


def _cell_position(reference: str) -> tuple[int, int]:
    column = 0
    index = 0
    while index < len(reference) and reference[index].isalpha():
        column = column * 26 + ord(reference[index].upper()) - ord("A") + 1
        index += 1
    row = int(reference[index:])
    return row, column


def _worksheet_images(
    archive: ZipFile,
    worksheet: ElementTree.Element,
    *,
    worksheet_path: str,
    worksheet_name: str,
    source_sheet_id: str,
    state: _ImageExtractionState,
) -> None:
    drawings = tuple(worksheet.iter(f"{{{_SPREADSHEET}}}drawing"))
    if not drawings:
        return
    if len(drawings) > 1:
        raise _document_error(
            "飞书电子表格工作表包含过多绘图关系",
            "drawing reference count limit exceeded",
        )
    relationships = _relationships(archive, worksheet_path)
    for drawing_reference in drawings:
        relationship_id = drawing_reference.get(_REL_ID)
        if not relationship_id:
            raise _document_error(
                "飞书电子表格图片关系无效",
                "drawing relationship id is missing",
            )
        drawing_path = _relationship_target(
            relationships,
            relationship_id=relationship_id,
            relationship_type="drawing",
        )
        if drawing_path in state.drawing_paths:
            raise _document_error(
                "飞书电子表格绘图关系重复",
                "duplicate drawing target rejected",
            )
        state.drawing_paths.add(drawing_path)
        drawing = _parse_xml(_read_member(archive, drawing_path, MAX_XML_BYTES))
        _require_xml_root(drawing, f"{{{_DRAWING}}}wsDr", "drawing")
        drawing_relationships = _relationships(archive, drawing_path)
        for anchor_node in tuple(drawing):
            blip = anchor_node.find(f".//{{{_DRAWING_MAIN}}}blip")
            if blip is None:
                continue
            image_relationship_id = blip.get(_REL_EMBED)
            if not image_relationship_id:
                continue
            media_path = _relationship_target(
                drawing_relationships,
                relationship_id=image_relationship_id,
                relationship_type="image",
            )
            state.anchor_count += 1
            if state.anchor_count > MAX_XLSX_ANCHOR_COUNT:
                raise _document_error(
                    "飞书电子表格图片锚点过多",
                    "xlsx image anchor count limit exceeded",
                )
            media_part = state.media_by_path.get(media_path)
            if media_part is None:
                if len(state.media_by_path) >= MAX_XLSX_MEDIA_COUNT:
                    raise _document_error(
                        "飞书电子表格图片条目过多",
                        "xlsx media count limit exceeded",
                    )
                media = _read_member(
                    archive,
                    media_path,
                    MAX_XLSX_MEDIA_BYTES,
                    oversized_detail="xlsx media bytes limit exceeded",
                )
                sha256 = hashlib.sha256(media).hexdigest()
                media_part = (media, sha256)
                state.media_by_path[media_path] = media_part
            media, sha256 = media_part
            start = anchor_node.find(f"{{{_DRAWING}}}from")
            row = _integer_child(start, "row")
            column = _integer_child(start, "col")
            anchor = SheetImageAnchor(
                row=row,
                column=column,
                media_name=posixpath.basename(media_path),
                sha256=sha256,
                worksheet_name=worksheet_name,
                source_sheet_id=source_sheet_id,
            )
            record = state.images_by_hash.get(sha256)
            if record is None:
                record = _ImageRecord(
                    media_name=posixpath.basename(media_path),
                    content=media,
                    anchors=[],
                )
                state.images_by_hash[sha256] = record
            record.anchors.append(anchor)


def _integer_child(parent: ElementTree.Element | None, name: str) -> int:
    if parent is None:
        return 0
    child = parent.find(f"{{{_DRAWING}}}{name}")
    return int(child.text) if child is not None and child.text is not None else 0


def _parse_xml(content: bytes) -> ElementTree.Element:
    if len(content) > MAX_XML_BYTES:
        raise _document_error(
            "飞书电子表格 XML 片段过大",
            "xml bytes limit exceeded",
        )
    if _has_forbidden_xml_declaration(content):
        raise _document_error(
            "飞书电子表格 XML 包含不安全实体",
            "xml entity declaration rejected",
        )

    parser = ElementTree.XMLPullParser(events=("start", "end"))
    root: ElementTree.Element | None = None
    depth = 0
    node_count = 0
    try:
        for offset in range(0, len(content), 64 * 1024):
            parser.feed(content[offset : offset + 64 * 1024])
            root, depth, node_count = _consume_xml_events(
                parser,
                root=root,
                depth=depth,
                node_count=node_count,
            )
        parser.close()
        root, depth, node_count = _consume_xml_events(
            parser,
            root=root,
            depth=depth,
            node_count=node_count,
        )
    except ElementTree.ParseError as exc:
        raise _document_error(
            "飞书电子表格 XML 格式无效",
            "malformed xml",
        ) from exc
    if root is None or depth != 0:
        raise _document_error(
            "飞书电子表格 XML 格式无效",
            "malformed xml",
        )
    return root


def _has_forbidden_xml_declaration(content: bytes) -> bool:
    upper_content = content.upper()
    declarations = ("<!DOCTYPE", "<!ENTITY")
    for declaration in declarations:
        if declaration.encode() in upper_content:
            return True
        for encoding in ("utf-16le", "utf-16be", "utf-32le", "utf-32be"):
            if declaration.encode(encoding) in upper_content:
                return True
    return False


def _require_xml_root(
    root: ElementTree.Element,
    expected_tag: str,
    part_type: str,
) -> None:
    if root.tag != expected_tag:
        raise _document_error(
            "飞书电子表格关系目标类型无效",
            f"unexpected relationship target for {part_type}",
        )


def _consume_xml_events(
    parser: ElementTree.XMLPullParser,
    *,
    root: ElementTree.Element | None,
    depth: int,
    node_count: int,
) -> tuple[ElementTree.Element | None, int, int]:
    for event, element in parser.read_events():
        if event == "start":
            if root is None:
                root = element
            depth += 1
            node_count += 1
            if depth > MAX_XML_DEPTH:
                raise _document_error(
                    "飞书电子表格 XML 嵌套过深",
                    "xml depth limit exceeded",
                )
            if node_count > MAX_XML_NODES:
                raise _document_error(
                    "飞书电子表格 XML 节点过多",
                    "xml node limit exceeded",
                )
        else:
            depth -= 1
    return root, depth, node_count


def _validate_source_sheet_id(source_sheet_id: str) -> None:
    if (
        not isinstance(source_sheet_id, str)
        or len(source_sheet_id) > MAX_BLOCK_TOKEN_LENGTH
        or not _is_safe_token_component(source_sheet_id)
    ):
        raise ValueError("invalid source sheet id")


def _validate_api_token(value: str, label: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) > MAX_BLOCK_TOKEN_LENGTH
        or not _is_safe_token_component(value)
    ):
        raise ValueError(f"invalid {label}")


def _is_safe_token_component(value: str) -> bool:
    return bool(value) and all(
        character.isascii()
        and (character.isalnum() or character in {"-", "_"})
        for character in value
    )


def _response_token(payload: dict, field: str) -> str:
    data = payload.get("data")
    value = data.get(field) if isinstance(data, dict) else None
    try:
        _validate_api_token(value, field)
    except ValueError as exc:
        raise _document_error(
            "飞书电子表格导出响应包含无效 Token",
            "unsafe API token rejected",
        ) from exc
    return value


def _export_timeout() -> AgentError:
    return AgentError(
        ErrorDetail(
            category=ErrorCategory.TRANSIENT,
            message="飞书电子表格导出超时，请稍后重试",
            technical_detail="export polling deadline exceeded",
            retryable=True,
        )
    )


def _validate_archive(archive: ZipFile) -> None:
    entries = archive.infolist()
    if len(entries) > MAX_XLSX_ENTRY_COUNT:
        raise _document_error(
            "飞书电子表格文件条目过多",
            "xlsx entry count limit exceeded",
        )
    total_uncompressed = 0
    media_count = 0
    seen_paths: set[str] = set()
    for entry in entries:
        path = _validate_member_path(entry.filename)
        if path in seen_paths:
            raise _document_error(
                "飞书电子表格包含重复文件条目",
                "duplicate zip member rejected",
            )
        seen_paths.add(path)
        if entry.flag_bits & 0x1:
            raise _document_error(
                "飞书电子表格包含加密文件条目",
                "encrypted zip member rejected",
            )
        total_uncompressed += entry.file_size
        if total_uncompressed > MAX_XLSX_UNCOMPRESSED_BYTES:
            raise _document_error(
                "飞书电子表格解压后过大",
                "xlsx uncompressed bytes limit exceeded",
            )
        if path.startswith("xl/media/") and not entry.is_dir():
            media_count += 1
            if media_count > MAX_XLSX_MEDIA_COUNT:
                raise _document_error(
                    "飞书电子表格图片条目过多",
                    "xlsx media count limit exceeded",
                )
            if entry.file_size > MAX_XLSX_MEDIA_BYTES:
                raise _document_error(
                    "飞书电子表格单个图片过大",
                    "xlsx media bytes limit exceeded",
                )


def _validate_member_path(raw_path: str) -> str:
    if (
        not raw_path
        or "\\" in raw_path
        or "\x00" in raw_path
        or posixpath.isabs(raw_path)
        or ":" in raw_path.split("/", 1)[0]
    ):
        raise _document_error(
            "飞书电子表格包含不安全文件路径",
            "unsafe zip member rejected",
        )
    path = raw_path[:-1] if raw_path.endswith("/") else raw_path
    parts = path.split("/")
    if (
        not path
        or any(part in {"", ".", ".."} for part in parts)
        or posixpath.normpath(path) != path
    ):
        raise _document_error(
            "飞书电子表格包含不安全文件路径",
            "unsafe zip member rejected",
        )
    return path


def _read_member(
    archive: ZipFile,
    path: str,
    limit: int,
    *,
    oversized_detail: str | None = None,
) -> bytes:
    try:
        entry = archive.getinfo(path)
    except KeyError as exc:
        raise _document_error(
            "飞书电子表格缺少必要文件",
            "required zip member missing",
        ) from exc
    if entry.file_size > limit:
        detail = oversized_detail or (
            "xlsx media bytes limit exceeded"
            if path.startswith("xl/media/")
            else "xml bytes limit exceeded"
        )
        raise _document_error("飞书电子表格文件条目过大", detail)
    try:
        with archive.open(entry) as stream:
            content = stream.read(limit + 1)
    except (
        BadZipFile,
        EOFError,
        NotImplementedError,
        OSError,
        RuntimeError,
        zlib.error,
    ) as exc:
        raise _document_error(
            "飞书电子表格 ZIP 条目无法解压",
            "zip decompression failed",
        ) from exc
    if len(content) > limit or len(content) != entry.file_size:
        raise _document_error(
            "飞书电子表格文件条目大小无效",
            "zip member size limit exceeded",
        )
    return content


def _document_error(message: str, technical_detail: str) -> AgentError:
    return AgentError(
        ErrorDetail(
            category=ErrorCategory.DOCUMENT,
            message=message,
            technical_detail=technical_detail,
            retryable=False,
        )
    )
