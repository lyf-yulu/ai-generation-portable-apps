from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from io import BytesIO
import json
from zipfile import ZIP_DEFLATED, ZipFile

import httpx
import pytest

from feishu_generation_agent.config import Settings
from feishu_generation_agent.domain.errors import AgentError, ErrorCategory
from feishu_generation_agent.integrations import feishu_sheet_export
from feishu_generation_agent.integrations.feishu_client import FeishuClient
from feishu_generation_agent.integrations.feishu_sheet_export import (
    EXPORT_POLL_INTERVAL_SECONDS,
    EXPORT_TIMEOUT_SECONDS,
    MAX_BLOCK_TOKEN_LENGTH,
    MAX_XLSX_COMPRESSED_BYTES,
    MAX_XLSX_ANCHOR_COUNT,
    MAX_XLSX_ENTRY_COUNT,
    MAX_XLSX_MEDIA_BYTES,
    MAX_XLSX_MEDIA_COUNT,
    MAX_XLSX_TEXT_BYTES,
    MAX_XLSX_TEXT_CHARACTERS,
    MAX_XLSX_TEXT_LINES,
    MAX_XLSX_UNCOMPRESSED_BYTES,
    MAX_XML_DEPTH,
    MAX_XML_BYTES,
    MAX_XML_NODES,
    EmbeddedSheetRef,
    ExtractedSheet,
    FeishuSheetExporter,
    SheetImageAnchor,
    extract_sheet_xlsx,
    parse_sheet_block_token,
)

_NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS_PACKAGE_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_NS_STRICT_REL = "http://purl.oclc.org/ooxml/officeDocument/relationships"
_NS_FAKE_STRICT_PACKAGE_REL = "http://purl.oclc.org/ooxml/package/relationships"
_NS_SPREADSHEET = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_NS_DRAWING = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
_NS_DRAWING_MAIN = "http://schemas.openxmlformats.org/drawingml/2006/main"


def _xlsx_members() -> dict[str, bytes]:
    return {
        "_rels/.rels": f"""
            <Relationships xmlns="{_NS_PACKAGE_REL}">
              <Relationship Id="rIdWorkbook" Type="{_NS_REL}/officeDocument"
                            Target="xl/workbook.xml"/>
            </Relationships>
        """.encode(),
        "xl/workbook.xml": f"""
            <workbook xmlns="{_NS_SPREADSHEET}" xmlns:r="{_NS_REL}">
              <sheets>
                <sheet name="分镜" sheetId="17" r:id="rIdStory"/>
              </sheets>
            </workbook>
        """.encode(),
        "xl/_rels/workbook.xml.rels": f"""
            <Relationships xmlns="{_NS_PACKAGE_REL}">
              <Relationship Id="rIdStory" Type="{_NS_REL}/worksheet"
                            Target="worksheets/story-board.xml"/>
              <Relationship Id="rIdStrings" Type="{_NS_REL}/sharedStrings"
                            Target="strings/custom-shared.xml"/>
            </Relationships>
        """.encode(),
        "xl/strings/custom-shared.xml": f"""
            <sst xmlns="{_NS_SPREADSHEET}" count="2" uniqueCount="2">
              <si><t>镜头一</t></si>
              <si><r><t>人物</t></r><r><t>保持一致</t></r></si>
            </sst>
        """.encode(),
        "xl/worksheets/story-board.xml": f"""
            <worksheet xmlns="{_NS_SPREADSHEET}" xmlns:r="{_NS_REL}">
              <sheetData>
                <row r="2"><c r="B2" t="s"><v>0</v></c></row>
                <row r="4"><c r="C4" t="s"><v>1</v></c></row>
              </sheetData>
              <drawing r:id="rIdStoryDrawing"/>
            </worksheet>
        """.encode(),
        "xl/worksheets/_rels/story-board.xml.rels": f"""
            <Relationships xmlns="{_NS_PACKAGE_REL}">
              <Relationship Id="rIdStoryDrawing" Type="{_NS_REL}/drawing"
                            Target="../drawings/story-art.xml"/>
            </Relationships>
        """.encode(),
        "xl/drawings/story-art.xml": f"""
            <xdr:wsDr xmlns:xdr="{_NS_DRAWING}" xmlns:a="{_NS_DRAWING_MAIN}"
                      xmlns:r="{_NS_REL}">
              <xdr:twoCellAnchor>
                <xdr:from><xdr:col>1</xdr:col><xdr:row>2</xdr:row></xdr:from>
                <xdr:pic><xdr:blipFill><a:blip r:embed="rIdHero"/></xdr:blipFill></xdr:pic>
              </xdr:twoCellAnchor>
              <xdr:oneCellAnchor>
                <xdr:from><xdr:col>4</xdr:col><xdr:row>6</xdr:row></xdr:from>
                <xdr:pic><xdr:blipFill><a:blip r:embed="rIdReference"/></xdr:blipFill></xdr:pic>
              </xdr:oneCellAnchor>
              <xdr:oneCellAnchor>
                <xdr:from><xdr:col>3</xdr:col><xdr:row>8</xdr:row></xdr:from>
                <xdr:pic><xdr:blipFill><a:blip r:embed="rIdHeroCopy"/></xdr:blipFill></xdr:pic>
              </xdr:oneCellAnchor>
            </xdr:wsDr>
        """.encode(),
        "xl/drawings/_rels/story-art.xml.rels": f"""
            <Relationships xmlns="{_NS_PACKAGE_REL}">
              <Relationship Id="rIdHero" Type="{_NS_REL}/image"
                            Target="../media/hero.png"/>
              <Relationship Id="rIdReference" Type="{_NS_REL}/image"
                            Target="../media/reference.png"/>
              <Relationship Id="rIdHeroCopy" Type="{_NS_REL}/image"
                            Target="../media/hero-copy.png"/>
            </Relationships>
        """.encode(),
        "xl/media/hero.png": b"\x89PNG\r\nfictional-hero",
        "xl/media/hero-copy.png": b"\x89PNG\r\nfictional-hero",
        "xl/media/reference.png": b"\x89PNG\r\nfictional-reference",
    }


def _make_xlsx(members: dict[str, bytes] | None = None) -> bytes:
    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        for name, content in (members or _xlsx_members()).items():
            archive.writestr(name, content)
    return output.getvalue()


def _make_xlsx_entries(entries: list[tuple[str, bytes]]) -> bytes:
    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return output.getvalue()


def _set_first_zip_compression_method(content: bytes, method: int) -> bytes:
    mutated = bytearray(content)
    local_header = mutated.index(b"PK\x03\x04")
    central_header = mutated.index(b"PK\x01\x02")
    mutated[local_header + 8 : local_header + 10] = method.to_bytes(2, "little")
    mutated[central_header + 10 : central_header + 12] = method.to_bytes(
        2,
        "little",
    )
    return bytes(mutated)


def _corrupt_first_zip_payload(content: bytes) -> bytes:
    mutated = bytearray(content)
    local_header = mutated.index(b"PK\x03\x04")
    compressed_size = int.from_bytes(
        mutated[local_header + 18 : local_header + 22],
        "little",
    )
    filename_size = int.from_bytes(
        mutated[local_header + 26 : local_header + 28],
        "little",
    )
    extra_size = int.from_bytes(
        mutated[local_header + 28 : local_header + 30],
        "little",
    )
    data_start = local_header + 30 + filename_size + extra_size
    mutated[data_start + compressed_size // 2] ^= 0xFF
    return bytes(mutated)


def _assert_document_error(content: bytes, *, detail: str) -> None:
    with pytest.raises(AgentError) as raised:
        extract_sheet_xlsx(content, target_sheet_id="NuBUx5")

    assert raised.value.detail.category is ErrorCategory.DOCUMENT
    assert raised.value.detail.retryable is False
    assert detail in raised.value.detail.technical_detail


def _assert_canaries_absent_from_exception_chain(
    error: BaseException,
    canaries: tuple[str, ...],
) -> None:
    pending = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        for canary in canaries:
            assert canary not in str(current)
        for linked in (current.__cause__, current.__context__):
            if linked is not None:
                pending.append(linked)


def test_client_redacts_overlapping_sensitive_values_longest_first() -> None:
    redacted = FeishuClient._redact_text(
        "short-token-long and short-token",
        ("short-token", "short-token-long", "short-token"),
    )

    assert redacted == "[redacted] and [redacted]"


def test_parse_sheet_block_token_splits_on_final_delimiter() -> None:
    ref = parse_sheet_block_token(
        "C7tUs3k3fhoiybtWxzvcqN7Nn3b_NuBUx5"
    )

    assert ref == EmbeddedSheetRef(
        spreadsheet_token="C7tUs3k3fhoiybtWxzvcqN7Nn3b",
        sheet_id="NuBUx5",
    )


@pytest.mark.parametrize(
    "raw",
    [
        "C7tUs3k3fhoiybtWxzvcqN7Nn3b",
        "_NuBUx5",
        "C7tUs3k3fhoiybtWxzvcqN7Nn3b_",
        "spread/sheet_NuBUx5",
        "spreadsheet_NuB\\Ux5",
        f"{'x' * MAX_BLOCK_TOKEN_LENGTH}_s",
    ],
)
def test_parse_sheet_block_token_rejects_unsafe_values(raw: str) -> None:
    with pytest.raises(ValueError, match="sheet block token"):
        parse_sheet_block_token(raw)


def test_extract_sheet_xlsx_reads_single_exported_worksheet() -> None:
    extracted = extract_sheet_xlsx(
        _make_xlsx(),
        target_sheet_id="NuBUx5",
    )

    assert isinstance(extracted, ExtractedSheet)
    assert extracted.text_lines == (
        "[sheet:NuBUx5 worksheet:分镜 cell:B2] 镜头一",
        "[sheet:NuBUx5 worksheet:分镜 cell:C4] 人物保持一致",
    )
    assert len(extracted.images) == 2
    assert [image.content for image in extracted.images] == [
        b"\x89PNG\r\nfictional-hero",
        b"\x89PNG\r\nfictional-reference",
    ]
    assert [len(image.anchors) for image in extracted.images] == [2, 1]
    assert extracted.images[0].anchors == (
        SheetImageAnchor(
            row=2,
            column=1,
            media_name="hero.png",
            sha256=extracted.images[0].sha256,
            worksheet_name="分镜",
            source_sheet_id="NuBUx5",
        ),
        SheetImageAnchor(
            row=8,
            column=3,
            media_name="hero-copy.png",
            sha256=extracted.images[0].sha256,
            worksheet_name="分镜",
            source_sheet_id="NuBUx5",
        ),
    )


def test_extract_sheet_xlsx_rejects_multiple_valid_worksheets() -> None:
    members = _xlsx_members()
    members["xl/workbook.xml"] = f"""
        <workbook xmlns="{_NS_SPREADSHEET}" xmlns:r="{_NS_REL}">
          <sheets>
            <sheet name="分镜" sheetId="17" r:id="rIdStory"/>
            <sheet name="角色" sheetId="42" r:id="rIdCharacters"/>
          </sheets>
        </workbook>
    """.encode()
    members["xl/_rels/workbook.xml.rels"] = f"""
        <Relationships xmlns="{_NS_PACKAGE_REL}">
          <Relationship Id="rIdStory" Type="{_NS_REL}/worksheet"
                        Target="worksheets/story-board.xml"/>
          <Relationship Id="rIdCharacters" Type="{_NS_REL}/worksheet"
                        Target="worksheets/character-board.xml"/>
          <Relationship Id="rIdStrings" Type="{_NS_REL}/sharedStrings"
                        Target="strings/custom-shared.xml"/>
        </Relationships>
    """.encode()
    members["xl/worksheets/character-board.xml"] = f"""
        <worksheet xmlns="{_NS_SPREADSHEET}">
          <sheetData>
            <row r="1"><c r="A1" t="s"><v>1</v></c></row>
          </sheetData>
        </worksheet>
    """.encode()

    _assert_document_error(
        _make_xlsx(members),
        detail="worksheet count must be exactly one",
    )


def test_xlsx_resource_limit_constants_are_positive_and_bounded() -> None:
    assert 0 < MAX_XLSX_COMPRESSED_BYTES < MAX_XLSX_UNCOMPRESSED_BYTES
    assert 0 < MAX_XLSX_ENTRY_COUNT <= MAX_XML_NODES
    assert 0 < MAX_XLSX_MEDIA_COUNT < MAX_XLSX_ENTRY_COUNT
    assert 0 < MAX_XLSX_ANCHOR_COUNT <= MAX_XML_NODES
    assert 0 < MAX_XLSX_MEDIA_BYTES < MAX_XLSX_UNCOMPRESSED_BYTES
    assert 0 < MAX_XLSX_TEXT_LINES <= MAX_XML_NODES
    assert 0 < MAX_XLSX_TEXT_CHARACTERS < MAX_XLSX_UNCOMPRESSED_BYTES
    assert MAX_XLSX_TEXT_CHARACTERS < MAX_XLSX_TEXT_BYTES
    assert MAX_XLSX_TEXT_BYTES < MAX_XLSX_UNCOMPRESSED_BYTES
    assert 0 < MAX_XML_BYTES < MAX_XLSX_UNCOMPRESSED_BYTES
    assert 0 < MAX_XML_DEPTH < MAX_XML_NODES
    assert 0 < EXPORT_POLL_INTERVAL_SECONDS < EXPORT_TIMEOUT_SECONDS <= 300


@pytest.mark.parametrize(
    "member_name",
    [
        "../escape.xml",
        "/absolute.xml",
        "%2e%2e/escape.xml",
        "xl%2fescape.xml",
        "xl%5cescape.xml",
        "xl/%252e%252e/escape.xml",
        "C%3a/escape.xml",
        "part%3fquery.xml",
        "part%23fragment.xml",
    ],
)
def test_extract_sheet_xlsx_rejects_unsafe_zip_member_paths(
    member_name: str,
) -> None:
    members = _xlsx_members()
    members[member_name] = b"must-not-be-read"

    _assert_document_error(_make_xlsx(members), detail="unsafe zip member")


def test_extract_sheet_xlsx_rejects_duplicate_zip_members() -> None:
    entries = list(_xlsx_members().items())
    entries.append(("xl/workbook.xml", b"<replacement/>"))

    with pytest.warns(UserWarning, match="Duplicate name"):
        content = _make_xlsx_entries(entries)
    _assert_document_error(content, detail="duplicate zip member")


def test_extract_sheet_xlsx_maps_unsupported_zip_compression_safely() -> None:
    content = _set_first_zip_compression_method(_make_xlsx(), 99)

    _assert_document_error(content, detail="zip decompression failed")


def test_extract_sheet_xlsx_maps_corrupt_deflate_payload_safely() -> None:
    content = _corrupt_first_zip_payload(_make_xlsx())

    _assert_document_error(content, detail="zip decompression failed")


@pytest.mark.parametrize(
    ("limit_name", "limit_value", "detail"),
    [
        ("MAX_XLSX_COMPRESSED_BYTES", 10, "compressed bytes limit"),
        ("MAX_XLSX_ENTRY_COUNT", 5, "entry count limit"),
        ("MAX_XLSX_MEDIA_COUNT", 2, "media count limit"),
        ("MAX_XLSX_MEDIA_BYTES", 8, "media bytes limit"),
        ("MAX_XLSX_UNCOMPRESSED_BYTES", 100, "uncompressed bytes limit"),
    ],
)
def test_extract_sheet_xlsx_enforces_archive_resource_limits(
    monkeypatch: pytest.MonkeyPatch,
    limit_name: str,
    limit_value: int,
    detail: str,
) -> None:
    monkeypatch.setattr(feishu_sheet_export, limit_name, limit_value)

    _assert_document_error(_make_xlsx(), detail=detail)


def test_extract_sheet_xlsx_rejects_escaping_relationship_targets() -> None:
    members = _xlsx_members()
    members["xl/worksheets/_rels/story-board.xml.rels"] = f"""
        <Relationships xmlns="{_NS_PACKAGE_REL}">
          <Relationship Id="rIdStoryDrawing" Type="{_NS_REL}/drawing"
                        Target="../../../../outside.xml"/>
        </Relationships>
    """.encode()

    _assert_document_error(_make_xlsx(members), detail="unsafe relationship target")


@pytest.mark.parametrize(
    "target",
    [
        "../%2e%2e/outside.xml",
        "..%2f../outside.xml",
        "..%5c../outside.xml",
        "../%252e%252e/outside.xml",
        "C%3a/escape.xml",
        "part%3fquery.xml",
        "part%23fragment.xml",
    ],
)
def test_extract_sheet_xlsx_rejects_percent_encoded_relationship_targets(
    target: str,
) -> None:
    members = _xlsx_members()
    members["xl/worksheets/_rels/story-board.xml.rels"] = f"""
        <Relationships xmlns="{_NS_PACKAGE_REL}">
          <Relationship Id="rIdStoryDrawing" Type="{_NS_REL}/drawing"
                        Target="{target}"/>
        </Relationships>
    """.encode()

    _assert_document_error(_make_xlsx(members), detail="unsafe relationship target")


def test_extract_sheet_xlsx_rejects_relationship_type_with_evil_prefix() -> None:
    members = _xlsx_members()
    members["_rels/.rels"] = f"""
        <Relationships xmlns="{_NS_PACKAGE_REL}">
          <Relationship Id="rIdWorkbook"
                        Type="https://evil.invalid/officeDocument"
                        Target="xl/workbook.xml"/>
        </Relationships>
    """.encode()

    _assert_document_error(_make_xlsx(members), detail="relationship type is invalid")


def test_extract_sheet_xlsx_rejects_non_relationships_xml_root() -> None:
    members = _xlsx_members()
    members["_rels/.rels"] = f"""
        <NotRelationships xmlns="{_NS_PACKAGE_REL}">
          <Relationship Id="rIdWorkbook" Type="{_NS_REL}/officeDocument"
                        Target="xl/workbook.xml"/>
        </NotRelationships>
    """.encode()

    _assert_document_error(_make_xlsx(members), detail="relationships root is invalid")


def test_extract_sheet_xlsx_accepts_strict_relationship_types() -> None:
    members = _xlsx_members()
    members["_rels/.rels"] = f"""
        <Relationships xmlns="{_NS_PACKAGE_REL}">
          <Relationship Id="rIdWorkbook"
                        Type="{_NS_STRICT_REL}/officeDocument"
                        Target="xl/workbook.xml"/>
        </Relationships>
    """.encode()
    members["xl/_rels/workbook.xml.rels"] = f"""
        <Relationships xmlns="{_NS_PACKAGE_REL}">
          <Relationship Id="rIdStory" Type="{_NS_STRICT_REL}/worksheet"
                        Target="worksheets/story-board.xml"/>
          <Relationship Id="rIdStrings" Type="{_NS_STRICT_REL}/sharedStrings"
                        Target="strings/custom-shared.xml"/>
        </Relationships>
    """.encode()
    members["xl/workbook.xml"] = f"""
        <workbook xmlns="{_NS_SPREADSHEET}" xmlns:r="{_NS_STRICT_REL}">
          <sheets>
            <sheet name="分镜" sheetId="17" r:id="rIdStory"/>
          </sheets>
        </workbook>
    """.encode()

    extracted = extract_sheet_xlsx(
        _make_xlsx(members),
        target_sheet_id="NuBUx5",
    )

    assert len(extracted.text_lines) == 2
    assert len(extracted.images) == 2


def test_extract_sheet_xlsx_rejects_nonstandard_package_relationship_namespace() -> None:
    members = _xlsx_members()
    members["_rels/.rels"] = f"""
        <Relationships xmlns="{_NS_FAKE_STRICT_PACKAGE_REL}">
          <Relationship Id="rIdWorkbook"
                        Type="{_NS_STRICT_REL}/officeDocument"
                        Target="xl/workbook.xml"/>
        </Relationships>
    """.encode()

    _assert_document_error(_make_xlsx(members), detail="relationships root is invalid")


def test_extract_sheet_xlsx_rejects_nonstandard_package_relationship_type() -> None:
    members = _xlsx_members()
    members["_rels/.rels"] = f"""
        <Relationships xmlns="{_NS_PACKAGE_REL}">
          <Relationship Id="rIdWorkbook" Type="{_NS_REL}/officeDocument"
                        Target="xl/workbook.xml"/>
          <Relationship Id="rIdCore"
                        Type="{_NS_FAKE_STRICT_PACKAGE_REL}/metadata/core-properties"
                        Target="docProps/core.xml"/>
        </Relationships>
    """.encode()

    _assert_document_error(_make_xlsx(members), detail="relationship type is invalid")


@pytest.mark.parametrize(
    "relationship_type",
    [
        f"{_NS_REL}/person",
        f"{_NS_STRICT_REL}/person",
        f"{_NS_REL}/threadedComment",
        f"{_NS_STRICT_REL}/threadedComment",
        f"{_NS_REL}/slicer",
        f"{_NS_STRICT_REL}/slicer",
        f"{_NS_REL}/timeline",
        f"{_NS_STRICT_REL}/timeline",
    ],
)
def test_extract_sheet_xlsx_rejects_invented_extension_relationship_types(
    relationship_type: str,
) -> None:
    members = _xlsx_members()
    members["_rels/.rels"] = f"""
        <Relationships xmlns="{_NS_PACKAGE_REL}">
          <Relationship Id="rIdWorkbook" Type="{_NS_REL}/officeDocument"
                        Target="xl/workbook.xml"/>
          <Relationship Id="rIdExtension" Type="{relationship_type}"
                        Target="extensions/ignored.xml"/>
        </Relationships>
    """.encode()

    _assert_document_error(_make_xlsx(members), detail="relationship type is invalid")


def test_extract_sheet_xlsx_safely_ignores_known_microsoft_extension_relationships() -> None:
    members = _xlsx_members()
    members["xl/_rels/workbook.xml.rels"] = f"""
        <Relationships xmlns="{_NS_PACKAGE_REL}">
          <Relationship Id="rIdStory" Type="{_NS_REL}/worksheet"
                        Target="worksheets/story-board.xml"/>
          <Relationship Id="rIdCharacters" Type="{_NS_REL}/worksheet"
                        Target="worksheets/character-board.xml"/>
          <Relationship Id="rIdStrings" Type="{_NS_REL}/sharedStrings"
                        Target="strings/custom-shared.xml"/>
          <Relationship Id="rIdPerson"
                        Type="http://schemas.microsoft.com/office/2017/10/relationships/person"
                        Target="persons/person.xml"/>
          <Relationship Id="rIdSlicerCache"
                        Type="http://schemas.microsoft.com/office/2007/relationships/slicerCache"
                        Target="slicerCaches/cache.xml"/>
          <Relationship Id="rIdTimelineCache"
                        Type="http://schemas.microsoft.com/office/2011/relationships/timelineCache"
                        Target="timelineCaches/cache.xml"/>
          <Relationship Id="rIdFormalTimelineCache"
                        Type="http://schemas.microsoft.com/office/2010/relationships/TimelineCache"
                        Target="timelineCaches/formal-cache.xml"/>
        </Relationships>
    """.encode()
    members["xl/worksheets/_rels/story-board.xml.rels"] = f"""
        <Relationships xmlns="{_NS_PACKAGE_REL}">
          <Relationship Id="rIdStoryDrawing" Type="{_NS_REL}/drawing"
                        Target="../drawings/story-art.xml"/>
          <Relationship Id="rIdSlicer"
                        Type="http://schemas.microsoft.com/office/2007/relationships/slicer"
                        Target="../slicers/slicer.xml"/>
          <Relationship Id="rIdTimeline"
                        Type="http://schemas.microsoft.com/office/2011/relationships/timeline"
                        Target="../timelines/timeline.xml"/>
          <Relationship Id="rIdFormalTimeline"
                        Type="http://schemas.microsoft.com/office/2010/relationships/Timeline"
                        Target="../timelines/formal-timeline.xml"/>
          <Relationship Id="rIdThreadedComment"
                        Type="http://schemas.microsoft.com/office/2017/10/relationships/threadedComment"
                        Target="../threadedcomments/comment.xml"/>
        </Relationships>
    """.encode()

    extracted = extract_sheet_xlsx(
        _make_xlsx(members),
        target_sheet_id="NuBUx5",
    )

    assert len(extracted.text_lines) == 2
    assert len(extracted.images) == 2


def test_extract_sheet_xlsx_rejects_misdirected_relationship_targets() -> None:
    members = _xlsx_members()
    members["xl/worksheets/_rels/story-board.xml.rels"] = f"""
        <Relationships xmlns="{_NS_PACKAGE_REL}">
          <Relationship Id="rIdStoryDrawing" Type="{_NS_REL}/drawing"
                        Target="../workbook.xml"/>
        </Relationships>
    """.encode()

    _assert_document_error(
        _make_xlsx(members),
        detail="unexpected relationship target",
    )


def test_extract_sheet_xlsx_ignores_unrelated_external_hyperlinks() -> None:
    members = _xlsx_members()
    members["xl/worksheets/_rels/story-board.xml.rels"] = f"""
        <Relationships xmlns="{_NS_PACKAGE_REL}">
          <Relationship Id="rIdStoryDrawing" Type="{_NS_REL}/drawing"
                        Target="../drawings/story-art.xml"/>
          <Relationship Id="rIdLink" Type="{_NS_REL}/hyperlink"
                        Target="https://example.invalid/"
                        TargetMode="External"/>
        </Relationships>
    """.encode()

    extracted = extract_sheet_xlsx(
        _make_xlsx(members),
        target_sheet_id="NuBUx5",
    )

    assert len(extracted.images) == 2
    assert len(extracted.text_lines) == 2


def test_extract_sheet_xlsx_skips_non_worksheet_sheet_relations() -> None:
    members = _xlsx_members()
    members["xl/workbook.xml"] = f"""
        <workbook xmlns="{_NS_SPREADSHEET}" xmlns:r="{_NS_REL}">
          <sheets>
            <sheet name="分镜" sheetId="17" r:id="rIdStory"/>
            <sheet name="图表" sheetId="23" r:id="rIdChart"/>
          </sheets>
        </workbook>
    """.encode()
    members["xl/_rels/workbook.xml.rels"] = f"""
        <Relationships xmlns="{_NS_PACKAGE_REL}">
          <Relationship Id="rIdStory" Type="{_NS_REL}/worksheet"
                        Target="worksheets/story-board.xml"/>
          <Relationship Id="rIdChart" Type="{_NS_REL}/chartsheet"
                        Target="chartsheets/overview.xml"/>
          <Relationship Id="rIdStrings" Type="{_NS_REL}/sharedStrings"
                        Target="strings/custom-shared.xml"/>
        </Relationships>
    """.encode()
    members["xl/chartsheets/overview.xml"] = (
        b"<chartsheet xmlns="
        b'"http://schemas.openxmlformats.org/spreadsheetml/2006/main"/>'
    )

    extracted = extract_sheet_xlsx(
        _make_xlsx(members),
        target_sheet_id="NuBUx5",
    )

    assert extracted.text_lines == (
        "[sheet:NuBUx5 worksheet:分镜 cell:B2] 镜头一",
        "[sheet:NuBUx5 worksheet:分镜 cell:C4] 人物保持一致",
    )


def test_extract_sheet_xlsx_resolves_media_outside_default_directory() -> None:
    members = _xlsx_members()
    hero = members.pop("xl/media/hero.png")
    members["custom/assets/hero.bin"] = hero
    members["xl/drawings/_rels/story-art.xml.rels"] = f"""
        <Relationships xmlns="{_NS_PACKAGE_REL}">
          <Relationship Id="rIdHero" Type="{_NS_REL}/image"
                        Target="../../custom/assets/hero.bin"/>
          <Relationship Id="rIdReference" Type="{_NS_REL}/image"
                        Target="../media/reference.png"/>
          <Relationship Id="rIdHeroCopy" Type="{_NS_REL}/image"
                        Target="../media/hero-copy.png"/>
        </Relationships>
    """.encode()

    extracted = extract_sheet_xlsx(
        _make_xlsx(members),
        target_sheet_id="NuBUx5",
    )

    assert extracted.images[0].media_name == "hero.bin"
    assert extracted.images[0].content == hero


def test_extract_sheet_xlsx_rejects_duplicate_relationship_id_when_incomplete() -> None:
    members = _xlsx_members()
    members["xl/worksheets/_rels/story-board.xml.rels"] = f"""
        <Relationships xmlns="{_NS_PACKAGE_REL}">
          <Relationship Id="rIdStoryDrawing"
                        Target="../drawings/ignored.xml"/>
          <Relationship Id="rIdStoryDrawing" Type="{_NS_REL}/drawing"
                        Target="../drawings/story-art.xml"/>
        </Relationships>
    """.encode()

    _assert_document_error(
        _make_xlsx(members),
        detail="duplicate relationship id",
    )


def test_extract_sheet_xlsx_rejects_worksheet_missing_relationship_id() -> None:
    members = _xlsx_members()
    members["xl/workbook.xml"] = f"""
        <workbook xmlns="{_NS_SPREADSHEET}" xmlns:r="{_NS_REL}">
          <sheets>
            <sheet name="分镜" sheetId="17"/>
            <sheet name="角色" sheetId="42" r:id="rIdCharacters"/>
          </sheets>
        </workbook>
    """.encode()

    _assert_document_error(
        _make_xlsx(members),
        detail="worksheet metadata is invalid",
    )


def test_extract_sheet_xlsx_rejects_drawing_missing_relationship_id() -> None:
    members = _xlsx_members()
    members["xl/worksheets/story-board.xml"] = f"""
        <worksheet xmlns="{_NS_SPREADSHEET}" xmlns:r="{_NS_REL}">
          <sheetData>
            <row r="2"><c r="B2" t="s"><v>0</v></c></row>
          </sheetData>
          <drawing/>
        </worksheet>
    """.encode()

    _assert_document_error(
        _make_xlsx(members),
        detail="drawing relationship id is missing",
    )


def test_extract_sheet_xlsx_rejects_blip_missing_embed_relationship() -> None:
    members = _xlsx_members()
    members["xl/drawings/story-art.xml"] = f"""
        <xdr:wsDr xmlns:xdr="{_NS_DRAWING}" xmlns:a="{_NS_DRAWING_MAIN}"
                  xmlns:r="{_NS_REL}">
          <xdr:oneCellAnchor>
            <xdr:from><xdr:col>1</xdr:col><xdr:row>2</xdr:row></xdr:from>
            <xdr:pic><xdr:blipFill><a:blip/></xdr:blipFill></xdr:pic>
          </xdr:oneCellAnchor>
        </xdr:wsDr>
    """.encode()

    _assert_document_error(
        _make_xlsx(members),
        detail="image relationship id is missing",
    )


def test_extract_sheet_xlsx_rejects_absolute_image_anchor() -> None:
    members = _xlsx_members()
    members["xl/drawings/story-art.xml"] = f"""
        <xdr:wsDr xmlns:xdr="{_NS_DRAWING}" xmlns:a="{_NS_DRAWING_MAIN}"
                  xmlns:r="{_NS_REL}">
          <xdr:absoluteAnchor>
            <xdr:pos x="0" y="0"/>
            <xdr:pic>
              <xdr:blipFill><a:blip r:embed="rIdHero"/></xdr:blipFill>
            </xdr:pic>
          </xdr:absoluteAnchor>
        </xdr:wsDr>
    """.encode()

    _assert_document_error(
        _make_xlsx(members),
        detail="image anchor type is unsupported",
    )


@pytest.mark.parametrize(
    "from_xml",
    [
        "",
        "<xdr:from><xdr:col>1</xdr:col></xdr:from>",
        "<xdr:from><xdr:row>2</xdr:row></xdr:from>",
    ],
)
def test_extract_sheet_xlsx_rejects_incomplete_image_anchor_position(
    from_xml: str,
) -> None:
    members = _xlsx_members()
    members["xl/drawings/story-art.xml"] = f"""
        <xdr:wsDr xmlns:xdr="{_NS_DRAWING}" xmlns:a="{_NS_DRAWING_MAIN}"
                  xmlns:r="{_NS_REL}">
          <xdr:oneCellAnchor>
            {from_xml}
            <xdr:pic>
              <xdr:blipFill><a:blip r:embed="rIdHero"/></xdr:blipFill>
            </xdr:pic>
          </xdr:oneCellAnchor>
        </xdr:wsDr>
    """.encode()

    _assert_document_error(
        _make_xlsx(members),
        detail="image anchor position is incomplete",
    )


def test_extract_sheet_xlsx_rejects_duplicate_drawing_references() -> None:
    members = _xlsx_members()
    members["xl/worksheets/story-board.xml"] = f"""
        <worksheet xmlns="{_NS_SPREADSHEET}" xmlns:r="{_NS_REL}">
          <sheetData>
            <row r="2"><c r="B2" t="s"><v>0</v></c></row>
          </sheetData>
          <drawing r:id="rIdStoryDrawing"/>
          <drawing r:id="rIdStoryDrawing"/>
        </worksheet>
    """.encode()

    _assert_document_error(
        _make_xlsx(members),
        detail="drawing reference count limit",
    )


def test_extract_sheet_xlsx_rejects_duplicate_worksheet_targets() -> None:
    members = _xlsx_members()
    members["xl/workbook.xml"] = f"""
        <workbook xmlns="{_NS_SPREADSHEET}" xmlns:r="{_NS_REL}">
          <sheets>
            <sheet name="分镜" sheetId="17" r:id="rIdStory"/>
            <sheet name="分镜副本" sheetId="18" r:id="rIdStory"/>
            <sheet name="角色" sheetId="42" r:id="rIdCharacters"/>
          </sheets>
        </workbook>
    """.encode()

    _assert_document_error(
        _make_xlsx(members),
        detail="duplicate worksheet target",
    )


def test_extract_sheet_xlsx_rejects_negative_shared_string_index() -> None:
    members = _xlsx_members()
    members["xl/worksheets/story-board.xml"] = f"""
        <worksheet xmlns="{_NS_SPREADSHEET}" xmlns:r="{_NS_REL}">
          <sheetData>
            <row r="2"><c r="B2" t="s"><v>-1</v></c></row>
          </sheetData>
          <drawing r:id="rIdStoryDrawing"/>
        </worksheet>
    """.encode()

    _assert_document_error(
        _make_xlsx(members),
        detail="shared string index is invalid",
    )


@pytest.mark.parametrize(
    ("limit_name", "limit_value", "detail"),
    [
        ("MAX_XLSX_TEXT_LINES", 2, "text line count limit"),
        ("MAX_XLSX_TEXT_CHARACTERS", 90, "text character limit"),
        ("MAX_XLSX_TEXT_BYTES", 100, "text utf-8 bytes limit"),
    ],
)
def test_extract_sheet_xlsx_bounds_repeated_shared_string_expansion(
    monkeypatch: pytest.MonkeyPatch,
    limit_name: str,
    limit_value: int,
    detail: str,
) -> None:
    members = _xlsx_members()
    members["xl/worksheets/story-board.xml"] = f"""
        <worksheet xmlns="{_NS_SPREADSHEET}">
          <sheetData>
            <row r="1">
              <c r="A1" t="s"><v>1</v></c>
              <c r="B1" t="s"><v>1</v></c>
              <c r="C1" t="s"><v>1</v></c>
            </row>
          </sheetData>
        </worksheet>
    """.encode()
    monkeypatch.setattr(feishu_sheet_export, limit_name, limit_value)

    _assert_document_error(_make_xlsx(members), detail=detail)


@pytest.mark.parametrize(
    "reference",
    [
        "AAAA1",
        "a1",
        "Ａ1",
        "A0",
        "A0000001",
        "XFE1",
        "A1048577",
        "A99999999",
        f"A{'9' * 100}",
        "A1A",
    ],
)
def test_extract_sheet_xlsx_rejects_invalid_or_out_of_bounds_cell_references(
    reference: str,
) -> None:
    members = _xlsx_members()
    members["xl/worksheets/story-board.xml"] = f"""
        <worksheet xmlns="{_NS_SPREADSHEET}">
          <sheetData>
            <row><c r="{reference}" t="s"><v>0</v></c></row>
          </sheetData>
        </worksheet>
    """.encode()

    _assert_document_error(_make_xlsx(members), detail="cell reference is invalid")


def test_extract_sheet_xlsx_accepts_maximum_excel_cell_reference() -> None:
    members = _xlsx_members()
    members["xl/worksheets/story-board.xml"] = f"""
        <worksheet xmlns="{_NS_SPREADSHEET}">
          <sheetData>
            <row r="1048576">
              <c r="XFD1048576" t="s"><v>0</v></c>
            </row>
          </sheetData>
        </worksheet>
    """.encode()

    extracted = extract_sheet_xlsx(
        _make_xlsx(members),
        target_sheet_id="NuBUx5",
    )

    assert extracted.text_lines[0] == (
        "[sheet:NuBUx5 worksheet:分镜 cell:XFD1048576] 镜头一"
    )


def test_extract_sheet_xlsx_rejects_missing_workbook_relationship() -> None:
    members = _xlsx_members()
    members["_rels/.rels"] = (
        f'<Relationships xmlns="{_NS_PACKAGE_REL}"/>'.encode()
    )

    _assert_document_error(_make_xlsx(members), detail="officeDocument")


def test_extract_sheet_xlsx_rejects_workbook_without_valid_worksheets() -> None:
    members = _xlsx_members()
    members["xl/workbook.xml"] = (
        f'<workbook xmlns="{_NS_SPREADSHEET}"><sheets/></workbook>'.encode()
    )

    _assert_document_error(_make_xlsx(members), detail="no valid worksheets")


def test_extract_sheet_xlsx_rejects_malformed_xml_safely() -> None:
    members = _xlsx_members()
    members["xl/workbook.xml"] = b"<workbook><broken></workbook>"

    _assert_document_error(_make_xlsx(members), detail="malformed xml")


def test_extract_sheet_xlsx_rejects_xml_entities_without_expansion() -> None:
    members = _xlsx_members()
    members["xl/workbook.xml"] = b"""<?xml version="1.0"?>
        <!DOCTYPE workbook [<!ENTITY secret "fictional-tenant-token">]>
        <workbook>&secret;</workbook>
    """

    _assert_document_error(_make_xlsx(members), detail="xml entity")


def test_extract_sheet_xlsx_rejects_utf16_xml_entities_without_expansion() -> None:
    members = _xlsx_members()
    members["xl/workbook.xml"] = """<?xml version="1.0" encoding="UTF-16"?>
        <!DOCTYPE workbook [<!ENTITY secret "fictional-tenant-token">]>
        <workbook>&secret;</workbook>
    """.encode("utf-16")

    _assert_document_error(_make_xlsx(members), detail="xml entity")


def test_extract_sheet_xlsx_rejects_excessive_xml_depth() -> None:
    members = _xlsx_members()
    nested = b"<x>" * (MAX_XML_DEPTH + 1) + b"</x>" * (MAX_XML_DEPTH + 1)
    members["xl/workbook.xml"] = (
        f'<workbook xmlns="{_NS_SPREADSHEET}">'.encode()
        + nested
        + b"</workbook>"
    )

    _assert_document_error(_make_xlsx(members), detail="xml depth limit")


def test_extract_sheet_xlsx_rejects_excessive_xml_nodes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(feishu_sheet_export, "MAX_XML_NODES", 4)

    _assert_document_error(_make_xlsx(), detail="xml node limit")


def test_extract_sheet_xlsx_rejects_excessive_image_anchors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(feishu_sheet_export, "MAX_XLSX_ANCHOR_COUNT", 2)

    _assert_document_error(_make_xlsx(), detail="image anchor count limit")


@pytest.mark.parametrize("target_sheet_id", ["", "../NuBUx5", "NuB\\Ux5"])
def test_extract_sheet_xlsx_rejects_unsafe_target_sheet_id(
    target_sheet_id: str,
) -> None:
    with pytest.raises(ValueError, match="target sheet id"):
        extract_sheet_xlsx(_make_xlsx(), target_sheet_id=target_sheet_id)


async def test_client_download_export_file_uses_authenticated_drive_path() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "tenant_access_token": "fiction-tenant-token",
                    "expire": 7200,
                },
            )
        return httpx.Response(
            200,
            content=b"fictional-xlsx",
            headers={"Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
        )

    async with httpx.AsyncClient(
        base_url="https://open.feishu.cn",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = FeishuClient(
            Settings(
                lark_app_id="fiction-app",
                lark_app_secret="fiction-app-secret",
            ),
            http_client=http_client,
        )
        content = await client.download_export_file(
            "fiction-file-token",
            max_bytes=1024,
        )

    assert content == b"fictional-xlsx"
    assert requests[-1].url.path == (
        "/open-apis/drive/v1/export_tasks/file/"
        "fiction-file-token/download"
    )
    assert requests[-1].headers["Authorization"] == (
        "Bearer fiction-tenant-token"
    )


@pytest.mark.parametrize(
    "file_token",
    [
        "",
        ".",
        "..",
        "../token",
        "nested/token",
        "token\\child",
        "token?secret=1",
        "token\nheader",
    ],
)
async def test_client_download_export_file_rejects_unsafe_path_token(
    file_token: str,
) -> None:
    async with httpx.AsyncClient(
        base_url="https://open.feishu.cn",
        transport=httpx.MockTransport(
            lambda request: pytest.fail(f"unexpected request: {request.url}")
        ),
    ) as http_client:
        client = FeishuClient(Settings(), http_client=http_client)
        with pytest.raises(ValueError, match="file token"):
            await client.download_export_file(file_token, max_bytes=1024)


async def test_client_download_export_file_enforces_size_limit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "tenant_access_token": "fiction-tenant-token",
                    "expire": 7200,
                },
            )
        return httpx.Response(200, content=b"oversized")

    async with httpx.AsyncClient(
        base_url="https://open.feishu.cn",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = FeishuClient(
            Settings(
                lark_app_id="fiction-app",
                lark_app_secret="fiction-app-secret",
            ),
            http_client=http_client,
        )
        with pytest.raises(AgentError) as raised:
            await client.download_export_file("fiction-file-token", max_bytes=4)

    assert raised.value.detail.category is ErrorCategory.DOCUMENT
    assert "size limit" in raised.value.detail.technical_detail


async def test_client_download_export_file_stops_stream_at_size_limit() -> None:
    class CountingStream(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self.chunks_read = 0

        async def __aiter__(self) -> AsyncIterator[bytes]:
            for chunk in (b"1234", b"5678", b"must-not-be-read"):
                self.chunks_read += 1
                yield chunk

    stream = CountingStream()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "tenant_access_token": "fiction-tenant-token",
                    "expire": 7200,
                },
            )
        return httpx.Response(
            200,
            stream=stream,
            headers={"Content-Type": "application/octet-stream"},
        )

    async with httpx.AsyncClient(
        base_url="https://open.feishu.cn",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = FeishuClient(
            Settings(
                lark_app_id="fiction-app",
                lark_app_secret="fiction-app-secret",
            ),
            http_client=http_client,
        )
        with pytest.raises(AgentError) as raised:
            await client.download_export_file(
                "fiction-file-token",
                max_bytes=6,
            )

    assert raised.value.detail.category is ErrorCategory.DOCUMENT
    assert stream.chunks_read == 2


async def test_client_download_export_file_preserves_primary_error_when_close_fails() -> None:
    canaries = (
        "spreadsheet-canary-token",
        "ticket-canary-token",
        "file-canary-token",
        "tenant-canary-token",
    )

    class FailingCloseStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b"oversized"

        async def aclose(self) -> None:
            raise httpx.ReadError(" ".join(canaries))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "tenant_access_token": "tenant-canary-token",
                    "expire": 7200,
                },
            )
        return httpx.Response(
            200,
            stream=FailingCloseStream(),
            headers={"Content-Type": "application/octet-stream"},
        )

    async with httpx.AsyncClient(
        base_url="https://open.feishu.cn",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = FeishuClient(
            Settings(
                lark_app_id="fiction-app",
                lark_app_secret="fiction-app-secret",
            ),
            http_client=http_client,
        )
        with pytest.raises(AgentError) as raised:
            await client.download_export_file(
                "file-canary-token",
                max_bytes=4,
                sensitive_values=(
                    "spreadsheet-canary-token",
                    "ticket-canary-token",
                ),
            )

    assert raised.value.detail.category is ErrorCategory.DOCUMENT
    assert "size limit" in raised.value.detail.technical_detail
    _assert_canaries_absent_from_exception_chain(raised.value, canaries)
    for canary in canaries:
        assert canary not in raised.value.detail.technical_detail


async def test_client_download_export_file_maps_midstream_transport_error() -> None:
    tenant_token = "fiction-tenant-token-must-not-leak"

    class FailingStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b"partial"
            raise httpx.ReadError(tenant_token)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "tenant_access_token": tenant_token,
                    "expire": 7200,
                },
            )
        return httpx.Response(
            200,
            stream=FailingStream(),
            headers={"Content-Type": "application/octet-stream"},
        )

    async with httpx.AsyncClient(
        base_url="https://open.feishu.cn",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = FeishuClient(
            Settings(
                lark_app_id="fiction-app",
                lark_app_secret="fiction-app-secret",
            ),
            http_client=http_client,
        )
        with pytest.raises(AgentError) as raised:
            await client.download_export_file(
                "fiction-file-token",
                max_bytes=1024,
            )

    assert raised.value.detail.category is ErrorCategory.TRANSIENT
    assert raised.value.detail.retryable is True
    assert tenant_token not in raised.value.detail.technical_detail


async def test_client_download_export_file_does_not_leak_tenant_token() -> None:
    tenant_token = "fiction-tenant-token-must-not-leak"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "tenant_access_token": tenant_token,
                    "expire": 7200,
                },
            )
        return httpx.Response(
            400,
            json={"code": 1770001, "msg": f"rejected {tenant_token}"},
        )

    async with httpx.AsyncClient(
        base_url="https://open.feishu.cn",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = FeishuClient(
            Settings(
                lark_app_id="fiction-app",
                lark_app_secret="fiction-app-secret",
            ),
            http_client=http_client,
        )
        with pytest.raises(AgentError) as raised:
            await client.download_export_file(
                tenant_token,
                max_bytes=1024,
            )

    assert tenant_token not in str(raised.value)
    assert tenant_token not in raised.value.detail.technical_detail


async def test_exporter_creates_polls_and_downloads_xlsx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api_requests: list[httpx.Request] = []
    status_requests = 0
    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal status_requests
        api_requests.append(request)
        if request.url.path.endswith("tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "tenant_access_token": "fiction-tenant-token",
                    "expire": 7200,
                },
            )
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"code": 0, "msg": "success", "data": {"ticket": "ticket-123"}},
            )
        if request.url.path.endswith("/ticket-123"):
            status_requests += 1
            status = 1 if status_requests == 1 else 0
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "msg": "success",
                    "data": {
                        "result": {
                            "extra": [],
                            "file_extension": "xlsx",
                            "file_name": "fictional.xlsx",
                            "file_size": len(_make_xlsx()),
                            "file_token": "fiction-file-token" if status == 0 else "",
                            "job_error_msg": "",
                            "job_status": status,
                            "type": "sheet",
                        }
                    },
                },
            )
        return httpx.Response(
            200,
            content=_make_xlsx(),
            headers={"Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
        )

    monkeypatch.setattr(feishu_sheet_export.asyncio, "sleep", fake_sleep)
    async with httpx.AsyncClient(
        base_url="https://open.feishu.cn",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = FeishuClient(
            Settings(
                lark_app_id="fiction-app",
                lark_app_secret="fiction-app-secret",
            ),
            http_client=http_client,
        )
        extracted = await FeishuSheetExporter(client).export(
            EmbeddedSheetRef(
                spreadsheet_token="C7tUs3k3fhoiybtWxzvcqN7Nn3b",
                sheet_id="NuBUx5",
            )
        )

    create_request = next(
        request
        for request in api_requests
        if request.url.path == "/open-apis/drive/v1/export_tasks"
        and request.method == "POST"
    )
    assert json.loads(create_request.content) == {
        "file_extension": "xlsx",
        "sub_id": "NuBUx5",
        "token": "C7tUs3k3fhoiybtWxzvcqN7Nn3b",
        "type": "sheet",
    }
    poll_requests = [
        request
        for request in api_requests
        if request.url.path.endswith("/export_tasks/ticket-123")
    ]
    assert len(poll_requests) == 2
    assert all(
        request.url.params.get("token")
        == "C7tUs3k3fhoiybtWxzvcqN7Nn3b"
        for request in poll_requests
    )
    assert api_requests[-1].url.path.endswith(
        "/export_tasks/file/fiction-file-token/download"
    )
    assert sleep_calls == [feishu_sheet_export.EXPORT_POLL_INTERVAL_SECONDS]
    assert extracted.text_lines[0].endswith("镜头一")
    assert len(extracted.images) == 2


@pytest.mark.parametrize(
    ("job_status", "expected_category", "expected_retryable"),
    [
        (3, ErrorCategory.DOCUMENT, False),
        (99, ErrorCategory.DOCUMENT, False),
    ],
)
async def test_exporter_rejects_failure_status_without_leaking_result(
    job_status: int,
    expected_category: ErrorCategory,
    expected_retryable: bool,
) -> None:
    secret = "fiction-tenant-token-must-not-leak"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "tenant_access_token": secret,
                    "expire": 7200,
                },
            )
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"code": 0, "msg": "success", "data": {"ticket": "ticket-123"}},
            )
        return httpx.Response(
            200,
            json={
                "code": 0,
                "msg": "success",
                "data": {
                    "result": {
                        "extra": [],
                        "file_extension": "xlsx",
                        "file_name": "",
                        "file_size": 0,
                        "file_token": "",
                        "job_error_msg": secret,
                        "job_status": job_status,
                        "type": "sheet",
                    }
                },
            },
        )

    async with httpx.AsyncClient(
        base_url="https://open.feishu.cn",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = FeishuClient(
            Settings(
                lark_app_id="fiction-app",
                lark_app_secret="fiction-app-secret",
            ),
            http_client=http_client,
        )
        with pytest.raises(AgentError) as raised:
            await FeishuSheetExporter(client).export(
                EmbeddedSheetRef("fiction-sheet-token", "NuBUx5")
            )

    assert raised.value.detail.category is expected_category
    assert raised.value.detail.retryable is expected_retryable
    assert secret not in str(raised.value)
    assert secret not in raised.value.detail.technical_detail


async def test_exporter_stops_at_polling_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock_values = iter((0.0, 0.0, 2.0))
    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "tenant_access_token": "fiction-tenant-token",
                    "expire": 7200,
                },
            )
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"code": 0, "msg": "success", "data": {"ticket": "ticket-123"}},
            )
        return httpx.Response(
            200,
            json={
                "code": 0,
                "msg": "success",
                "data": {
                    "result": {
                        "extra": [],
                        "file_extension": "xlsx",
                        "file_name": "",
                        "file_size": 0,
                        "file_token": "",
                        "job_error_msg": "",
                        "job_status": 2,
                        "type": "sheet",
                    }
                },
            },
        )

    monkeypatch.setattr(feishu_sheet_export, "EXPORT_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(
        feishu_sheet_export,
        "monotonic",
        lambda: next(clock_values),
    )
    monkeypatch.setattr(feishu_sheet_export.asyncio, "sleep", fake_sleep)
    async with httpx.AsyncClient(
        base_url="https://open.feishu.cn",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = FeishuClient(
            Settings(
                lark_app_id="fiction-app",
                lark_app_secret="fiction-app-secret",
            ),
            http_client=http_client,
        )
        with pytest.raises(AgentError) as raised:
            await FeishuSheetExporter(client).export(
                EmbeddedSheetRef("fiction-sheet-token", "NuBUx5")
            )

    assert raised.value.detail.category is ErrorCategory.TRANSIENT
    assert raised.value.detail.retryable is True
    assert "deadline" in raised.value.detail.technical_detail
    assert sleep_calls == []


async def test_exporter_bounds_status_request_by_remaining_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    never_respond = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "tenant_access_token": "fiction-tenant-token",
                    "expire": 7200,
                },
            )
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"code": 0, "msg": "success", "data": {"ticket": "ticket-123"}},
            )
        await never_respond.wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(feishu_sheet_export, "EXPORT_TIMEOUT_SECONDS", 0.01)
    async with httpx.AsyncClient(
        base_url="https://open.feishu.cn",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = FeishuClient(
            Settings(
                lark_app_id="fiction-app",
                lark_app_secret="fiction-app-secret",
            ),
            http_client=http_client,
        )
        with pytest.raises(AgentError) as raised:
            await asyncio.wait_for(
                FeishuSheetExporter(client).export(
                    EmbeddedSheetRef("fiction-sheet-token", "NuBUx5")
                ),
                timeout=0.2,
            )

    assert raised.value.detail.category is ErrorCategory.TRANSIENT
    assert "deadline" in raised.value.detail.technical_detail


async def test_exporter_total_deadline_bounds_create_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    never_respond = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "tenant_access_token": "fiction-tenant-token",
                    "expire": 7200,
                },
            )
        await never_respond.wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(feishu_sheet_export, "EXPORT_TIMEOUT_SECONDS", 0.01)
    async with httpx.AsyncClient(
        base_url="https://open.feishu.cn",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = FeishuClient(
            Settings(
                lark_app_id="fiction-app",
                lark_app_secret="fiction-app-secret",
            ),
            http_client=http_client,
        )
        with pytest.raises(AgentError) as raised:
            await asyncio.wait_for(
                FeishuSheetExporter(client).export(
                    EmbeddedSheetRef("fiction-sheet-token", "NuBUx5")
                ),
                timeout=0.2,
            )

    assert raised.value.detail.category is ErrorCategory.TRANSIENT
    assert "deadline" in raised.value.detail.technical_detail


async def test_exporter_total_deadline_bounds_download_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    never_respond = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "tenant_access_token": "fiction-tenant-token",
                    "expire": 7200,
                },
            )
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"code": 0, "data": {"ticket": "ticket-123"}},
            )
        if request.url.path.endswith("/ticket-123"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "result": {
                            "job_status": 0,
                            "file_token": "fiction-file-token",
                        }
                    },
                },
            )
        await never_respond.wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(feishu_sheet_export, "EXPORT_TIMEOUT_SECONDS", 0.01)
    async with httpx.AsyncClient(
        base_url="https://open.feishu.cn",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = FeishuClient(
            Settings(
                lark_app_id="fiction-app",
                lark_app_secret="fiction-app-secret",
            ),
            http_client=http_client,
        )
        with pytest.raises(AgentError) as raised:
            await asyncio.wait_for(
                FeishuSheetExporter(client).export(
                    EmbeddedSheetRef("fiction-sheet-token", "NuBUx5")
                ),
                timeout=0.2,
            )

    assert raised.value.detail.category is ErrorCategory.TRANSIENT
    assert "deadline" in raised.value.detail.technical_detail


async def test_exporter_total_deadline_bounds_continuous_download_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SlowEndlessStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            while True:
                await asyncio.sleep(0.005)
                yield b"x"

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "tenant_access_token": "fiction-tenant-token",
                    "expire": 7200,
                },
            )
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"code": 0, "data": {"ticket": "ticket-123"}},
            )
        if request.url.path.endswith("/ticket-123"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "result": {
                            "job_status": 0,
                            "file_token": "fiction-file-token",
                        }
                    },
                },
            )
        return httpx.Response(
            200,
            stream=SlowEndlessStream(),
            headers={"Content-Type": "application/octet-stream"},
        )

    monkeypatch.setattr(feishu_sheet_export, "EXPORT_TIMEOUT_SECONDS", 0.02)
    async with httpx.AsyncClient(
        base_url="https://open.feishu.cn",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = FeishuClient(
            Settings(
                lark_app_id="fiction-app",
                lark_app_secret="fiction-app-secret",
            ),
            http_client=http_client,
        )
        with pytest.raises(AgentError) as raised:
            await asyncio.wait_for(
                FeishuSheetExporter(client).export(
                    EmbeddedSheetRef("fiction-sheet-token", "NuBUx5")
                ),
                timeout=0.2,
            )

    assert raised.value.detail.category is ErrorCategory.TRANSIENT
    assert "deadline" in raised.value.detail.technical_detail


async def test_exporter_total_deadline_does_not_wait_for_hanging_stream_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    close_never_finishes = asyncio.Event()

    class HangingCloseStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            while True:
                await asyncio.sleep(0.005)
                yield b"x"

        async def aclose(self) -> None:
            await close_never_finishes.wait()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "tenant_access_token": "fiction-tenant-token",
                    "expire": 7200,
                },
            )
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"code": 0, "data": {"ticket": "ticket-123"}},
            )
        if request.url.path.endswith("/ticket-123"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "result": {
                            "job_status": 0,
                            "file_token": "fiction-file-token",
                        }
                    },
                },
            )
        return httpx.Response(
            200,
            stream=HangingCloseStream(),
            headers={"Content-Type": "application/octet-stream"},
        )

    monkeypatch.setattr(feishu_sheet_export, "EXPORT_TIMEOUT_SECONDS", 0.02)
    async with httpx.AsyncClient(
        base_url="https://open.feishu.cn",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = FeishuClient(
            Settings(
                lark_app_id="fiction-app",
                lark_app_secret="fiction-app-secret",
            ),
            http_client=http_client,
        )
        with pytest.raises(AgentError) as raised:
            await asyncio.wait_for(
                FeishuSheetExporter(client).export(
                    EmbeddedSheetRef("fiction-sheet-token", "NuBUx5")
                ),
                timeout=0.15,
            )

    assert raised.value.detail.category is ErrorCategory.TRANSIENT
    assert "deadline" in raised.value.detail.technical_detail


async def _export_error_from_handler(
    handler: object,
) -> AgentError:
    async with httpx.AsyncClient(
        base_url="https://open.feishu.cn",
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
    ) as http_client:
        client = FeishuClient(
            Settings(
                lark_app_id="fiction-app",
                lark_app_secret="fiction-app-secret",
            ),
            http_client=http_client,
        )
        with pytest.raises(AgentError) as raised:
            await FeishuSheetExporter(client).export(
                EmbeddedSheetRef(
                    "spreadsheet-canary-token",
                    "NuBUx5",
                )
            )
    return raised.value


def _assert_export_canaries_redacted(error: AgentError) -> None:
    canaries = (
        "spreadsheet-canary-token",
        "ticket-canary-token",
        "file-canary-token",
        "tenant-canary-token",
    )
    _assert_canaries_absent_from_exception_chain(error, canaries)
    for canary in canaries:
        assert canary not in str(error)
        assert canary not in error.detail.technical_detail


async def test_exporter_redacts_spreadsheet_and_tenant_tokens_from_server_msg() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "tenant_access_token": "tenant-canary-token",
                    "expire": 7200,
                },
            )
        return httpx.Response(
            400,
            json={
                "code": 1770001,
                "msg": (
                    "spreadsheet-canary-token "
                    "tenant-canary-token"
                ),
            },
        )

    error = await _export_error_from_handler(handler)

    _assert_export_canaries_redacted(error)


async def test_exporter_redacts_ticket_from_poll_path_and_server_msg() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "tenant_access_token": "tenant-canary-token",
                    "expire": 7200,
                },
            )
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {"ticket": "ticket-canary-token"},
                },
            )
        return httpx.Response(
            400,
            json={
                "code": 1770001,
                "msg": "ticket-canary-token",
            },
        )

    error = await _export_error_from_handler(handler)

    _assert_export_canaries_redacted(error)


async def test_exporter_redacts_file_token_from_download_api_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "tenant_access_token": "tenant-canary-token",
                    "expire": 7200,
                },
            )
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {"ticket": "ticket-canary-token"},
                },
            )
        if request.url.path.endswith("/ticket-canary-token"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "result": {
                            "job_status": 0,
                            "file_token": "file-canary-token",
                        }
                    },
                },
            )
        return httpx.Response(
            400,
            json={
                "code": 1770001,
                "msg": (
                    "spreadsheet-canary-token ticket-canary-token "
                    "file-canary-token tenant-canary-token"
                ),
            },
        )

    error = await _export_error_from_handler(handler)

    _assert_export_canaries_redacted(error)


async def test_exporter_redacts_all_tokens_from_download_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "tenant_access_token": "tenant-canary-token",
                    "expire": 7200,
                },
            )
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {"ticket": "ticket-canary-token"},
                },
            )
        if request.url.path.endswith("/ticket-canary-token"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "result": {
                            "job_status": 0,
                            "file_token": "file-canary-token",
                        }
                    },
                },
            )
        raise httpx.ConnectError(
            (
                "spreadsheet-canary-token ticket-canary-token "
                "file-canary-token tenant-canary-token"
            ),
            request=request,
        )

    error = await _export_error_from_handler(handler)

    _assert_export_canaries_redacted(error)


@pytest.mark.parametrize(
    ("response_field", "unsafe_value"),
    [
        ("ticket", "../ticket"),
        ("ticket", ".."),
        ("file_token", "../file-token"),
        ("file_token", ".."),
    ],
)
async def test_exporter_rejects_unsafe_api_path_tokens(
    response_field: str,
    unsafe_value: str,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "tenant_access_token": "fiction-tenant-token",
                    "expire": 7200,
                },
            )
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "msg": "success",
                    "data": {
                        "ticket": (
                            unsafe_value
                            if response_field == "ticket"
                            else "ticket-123"
                        )
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "code": 0,
                "msg": "success",
                "data": {
                    "result": {
                        "extra": [],
                        "file_extension": "xlsx",
                        "file_name": "fictional.xlsx",
                        "file_size": 1,
                        "file_token": (
                            unsafe_value
                            if response_field == "file_token"
                            else "fiction-file-token"
                        ),
                        "job_error_msg": "",
                        "job_status": 0,
                        "type": "sheet",
                    }
                },
            },
        )

    async with httpx.AsyncClient(
        base_url="https://open.feishu.cn",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = FeishuClient(
            Settings(
                lark_app_id="fiction-app",
                lark_app_secret="fiction-app-secret",
            ),
            http_client=http_client,
        )
        with pytest.raises(AgentError) as raised:
            await FeishuSheetExporter(client).export(
                EmbeddedSheetRef("fiction-sheet-token", "NuBUx5")
            )

    assert raised.value.detail.category is ErrorCategory.DOCUMENT
    assert "unsafe API token" in raised.value.detail.technical_detail
    assert not any("/../" in request.url.path for request in requests)
