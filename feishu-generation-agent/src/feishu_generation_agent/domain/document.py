import hashlib
import re
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


BLOCKING_INGEST_ISSUE_PREFIX = "阻塞："
NON_BLOCKING_INGEST_ISSUE_PREFIX = "素材失败："
_SAFE_ISSUE_ID = r"[A-Za-z0-9_-]{1,1024}"


class IngestIssueSeverity(StrEnum):
    BLOCKING = "blocking"
    ASSET = "asset"


class IngestIssueCode(StrEnum):
    SHEET_REFERENCE_INVALID = "sheet_reference_invalid"
    SHEET_EXPORTER_UNAVAILABLE = "sheet_exporter_unavailable"
    SHEET_EXPORT_TIMEOUT = "sheet_export_timeout"
    SHEET_EXPORT_FAILED = "sheet_export_failed"
    SHEET_EXPORT_EMPTY = "sheet_export_empty"
    SHEET_ASSET_SAVE_FAILED = "sheet_asset_save_failed"
    MEDIA_DOWNLOAD_FAILED = "media_download_failed"
    LEGACY_SHEET_READ_FAILED = "legacy_sheet_read_failed"
    LEGACY_UNKNOWN = "legacy_unknown"


_INGEST_ISSUE_SPECS = {
    IngestIssueCode.SHEET_REFERENCE_INVALID: (
        IngestIssueSeverity.BLOCKING,
        "飞书电子表格引用无效，请检查文档后重试",
    ),
    IngestIssueCode.SHEET_EXPORTER_UNAVAILABLE: (
        IngestIssueSeverity.BLOCKING,
        "飞书电子表格读取服务未配置，请联系管理员",
    ),
    IngestIssueCode.SHEET_EXPORT_TIMEOUT: (
        IngestIssueSeverity.BLOCKING,
        "飞书电子表格导出超时，请稍后重试",
    ),
    IngestIssueCode.SHEET_EXPORT_FAILED: (
        IngestIssueSeverity.BLOCKING,
        "飞书电子表格导出失败，请稍后重试",
    ),
    IngestIssueCode.SHEET_EXPORT_EMPTY: (
        IngestIssueSeverity.BLOCKING,
        "飞书电子表格没有可读取的文字或图片，请检查后重试",
    ),
    IngestIssueCode.SHEET_ASSET_SAVE_FAILED: (
        IngestIssueSeverity.ASSET,
        "电子表格图片保存失败，其他素材可继续处理",
    ),
    IngestIssueCode.MEDIA_DOWNLOAD_FAILED: (
        IngestIssueSeverity.ASSET,
        "文档图片下载失败，其他素材可继续处理",
    ),
    IngestIssueCode.LEGACY_SHEET_READ_FAILED: (
        IngestIssueSeverity.BLOCKING,
        "飞书电子表格读取失败，请重新读取后再审批",
    ),
    IngestIssueCode.LEGACY_UNKNOWN: (
        IngestIssueSeverity.BLOCKING,
        "文档读取出现未知问题，请重新读取后再审批",
    ),
}


def _issue_identifier_is_safe(identifier: str) -> bool:
    lowered = identifier.lower()
    return (
        not any(marker in lowered for marker in ("bearer", "token", "secret"))
        and re.search(
            r"(?:^|[-_])sk[-_][a-z0-9_-]{8,}",
            lowered,
        )
        is None
        and "ark-" not in lowered
        and "ark_" not in lowered
        and "aklt" not in lowered
    )


class IngestIssueRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    severity: IngestIssueSeverity
    code: IngestIssueCode
    display_message: str
    source_block_id: str | None = Field(
        default=None,
        pattern=rf"^{_SAFE_ISSUE_ID}$",
    )
    asset_id: str | None = Field(
        default=None,
        pattern=rf"^{_SAFE_ISSUE_ID}$",
    )

    @model_validator(mode="after")
    def validate_spec(self) -> "IngestIssueRecord":
        severity, message = _INGEST_ISSUE_SPECS[self.code]
        if self.severity is not severity or self.display_message != message:
            raise ValueError("ingest issue code, severity, and message do not match")
        for identifier in (self.source_block_id, self.asset_id):
            if identifier is not None and not _issue_identifier_is_safe(
                identifier
            ):
                raise ValueError("ingest issue identifier is not safe")
        return self

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        if not update:
            return super().model_copy(deep=deep)
        payload = self.model_dump()
        payload.update(update)
        return type(self).model_validate(payload)


def make_ingest_issue_record(
    code: IngestIssueCode,
    *,
    source_block_id: str | None = None,
    asset_id: str | None = None,
) -> IngestIssueRecord:
    severity, message = _INGEST_ISSUE_SPECS[code]
    return IngestIssueRecord(
        severity=severity,
        code=code,
        display_message=message,
        source_block_id=source_block_id,
        asset_id=asset_id,
    )


_LEGACY_SHEET_ASSET = re.compile(
    rf"^(?:阻塞：|素材失败：)内嵌电子表格素材 "
    rf"(?P<asset>{_SAFE_ISSUE_ID}) 保存失败"
    rf"(?:（Block {_SAFE_ISSUE_ID}，Sheet {_SAFE_ISSUE_ID}）"
    rf"：图片保存失败，请稍后重试)?$"
)
_LEGACY_SHEET_ASSET_GENERIC = re.compile(
    r"^(?:阻塞：|素材失败：)内嵌电子表格素材保存失败$"
)
_LEGACY_MEDIA_DOWNLOAD = re.compile(
    rf"^(?:阻塞：|素材失败：)素材 "
    rf"(?P<asset>{_SAFE_ISSUE_ID}) 下载失败"
    rf"(?:（Block {_SAFE_ISSUE_ID}）"
    rf"：图片下载或保存失败，请稍后重试)?$"
)
_LEGACY_SHEET_READ = re.compile(
    rf"^阻塞：内嵌电子表格(?: {_SAFE_ISSUE_ID})? 读取失败"
    rf"（Block (?P<block>{_SAFE_ISSUE_ID})）"
    rf"(?:：(?P<reason>[^：]+))?$"
)
_LEGACY_SHEET_READ_NO_SPACE = re.compile(
    rf"^阻塞：内嵌电子表格读取失败"
    rf"（Block (?P<block>{_SAFE_ISSUE_ID})）"
    rf"(?:：(?P<reason>[^：]+))?$"
)
_LEGACY_SAFE_REASONS = {
    "飞书电子表格导出超时，请稍后重试": IngestIssueCode.SHEET_EXPORT_TIMEOUT,
    "电子表格导出失败，请稍后重试": IngestIssueCode.SHEET_EXPORT_FAILED,
    "导出结果为空": IngestIssueCode.SHEET_EXPORT_EMPTY,
    "未配置电子表格导出器": IngestIssueCode.SHEET_EXPORTER_UNAVAILABLE,
    "Sheet Block 缺少 token": IngestIssueCode.SHEET_REFERENCE_INVALID,
    "电子表格引用无效": IngestIssueCode.SHEET_REFERENCE_INVALID,
}


def _migrate_legacy_ingest_issue(issue: str) -> IngestIssueRecord:
    matched = (
        _LEGACY_SHEET_ASSET.fullmatch(issue)
        or _LEGACY_SHEET_ASSET_GENERIC.fullmatch(issue)
    )
    if matched:
        asset_id = matched.groupdict().get("asset")
        if asset_id is not None and not _issue_identifier_is_safe(asset_id):
            return make_ingest_issue_record(IngestIssueCode.LEGACY_UNKNOWN)
        return make_ingest_issue_record(
            IngestIssueCode.SHEET_ASSET_SAVE_FAILED,
        )
    matched = _LEGACY_MEDIA_DOWNLOAD.fullmatch(issue)
    if matched:
        if not _issue_identifier_is_safe(matched.group("asset")):
            return make_ingest_issue_record(IngestIssueCode.LEGACY_UNKNOWN)
        return make_ingest_issue_record(
            IngestIssueCode.MEDIA_DOWNLOAD_FAILED,
            asset_id=matched.group("asset"),
        )
    matched = (
        _LEGACY_SHEET_READ.fullmatch(issue)
        or _LEGACY_SHEET_READ_NO_SPACE.fullmatch(issue)
    )
    if matched:
        if not _issue_identifier_is_safe(matched.group("block")):
            return make_ingest_issue_record(IngestIssueCode.LEGACY_UNKNOWN)
        reason = matched.group("reason")
        code = (
            _LEGACY_SAFE_REASONS.get(reason)
            if reason is not None
            else IngestIssueCode.LEGACY_SHEET_READ_FAILED
        )
        if code is not None:
            return make_ingest_issue_record(
                code,
                source_block_id=matched.group("block"),
            )
    return make_ingest_issue_record(IngestIssueCode.LEGACY_UNKNOWN)


def resolve_ingest_issue_records(document: Any) -> list[IngestIssueRecord]:
    records = (
        document.get("ingest_issue_records")
        if isinstance(document, Mapping)
        else getattr(document, "ingest_issue_records", None)
    )
    if isinstance(records, list) and records:
        return [
            IngestIssueRecord.model_validate(
                record.model_dump()
                if isinstance(record, IngestIssueRecord)
                else record
            )
            for record in records
        ]
    issues = (
        document.get("ingest_issues")
        if isinstance(document, Mapping)
        else getattr(document, "ingest_issues", None)
    )
    if not isinstance(issues, list):
        return []
    return [
        _migrate_legacy_ingest_issue(issue)
        for issue in issues
        if isinstance(issue, str)
    ]


def legacy_ingest_issue_text(record: IngestIssueRecord) -> str:
    if record.code is IngestIssueCode.SHEET_ASSET_SAVE_FAILED:
        return (
            f"{NON_BLOCKING_INGEST_ISSUE_PREFIX}"
            "内嵌电子表格素材保存失败"
        )
    if record.code is IngestIssueCode.MEDIA_DOWNLOAD_FAILED:
        return (
            f"{NON_BLOCKING_INGEST_ISSUE_PREFIX}"
            f"素材 {record.asset_id} 下载失败"
        )
    return (
        f"{BLOCKING_INGEST_ISSUE_PREFIX}"
        f"内嵌电子表格读取失败（Block {record.source_block_id or 'unknown'}）："
        f"{record.display_message}"
    )


class SourceType(StrEnum):
    DOCX = "docx"
    WIKI = "wiki"


class PlanningPromptSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    owner_user_id: str = Field(min_length=1, max_length=255)
    source: Literal["prime", "personal"]
    version: int = Field(ge=0)
    prompt_text: str = Field(min_length=1)
    prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_prompt_hash(self) -> "PlanningPromptSnapshot":
        expected = hashlib.sha256(self.prompt_text.encode("utf-8")).hexdigest()
        if self.prompt_sha256 != expected:
            raise ValueError("prompt_sha256 does not match prompt_text")
        return self

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        if not update:
            return super().model_copy(deep=deep)
        payload = self.model_dump()
        payload.update(update)
        return type(self).model_validate(payload)


def build_planning_prompt_snapshot(
    *,
    owner_user_id: str,
    source: Literal["prime", "personal"],
    version: int,
    prompt_text: str,
) -> PlanningPromptSnapshot:
    return PlanningPromptSnapshot(
        owner_user_id=owner_user_id,
        source=source,
        version=version,
        prompt_text=prompt_text,
        prompt_sha256=hashlib.sha256(prompt_text.encode("utf-8")).hexdigest(),
    )


class RequirementRequest(BaseModel):
    source_url: str
    requester_open_id: str | None = None
    trigger_type: str = "local_link"
    reply_context: dict[str, str] = Field(default_factory=dict)
    planning_prompt: PlanningPromptSnapshot | None = None


class DocumentBlock(BaseModel):
    block_id: str
    parent_id: str | None
    block_type: str
    order: int
    path: list[str]
    text: str = ""
    table_row: int | None = None
    table_column: int | None = None
    image_asset_id: str | None = None


class MediaAsset(BaseModel):
    asset_id: str
    source_block_id: str
    origin: str
    file_token: str | None = None
    local_path: Path
    mime_type: str
    size: int
    sha256: str
    width: int | None = None
    height: int | None = None
    download_error: str | None = None


class VisionDescription(BaseModel):
    asset_id: str
    subjects: list[str]
    scene: str
    style: str
    composition: str
    characters: list[str]
    actions: list[str]
    visible_text: list[str]
    colors: list[str]
    probable_role: str
    uncertainties: list[str]


class NormalizedDocument(BaseModel):
    document_id: str
    title: str
    revision: int
    source_type: SourceType
    source_token: str
    blocks: list[DocumentBlock]
    text_view: str
    media_assets: list[MediaAsset]
    ingest_issue_records: list[IngestIssueRecord] = Field(default_factory=list)
    ingest_issues: list[str] = Field(default_factory=list)
