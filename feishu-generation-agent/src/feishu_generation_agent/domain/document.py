from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field


class SourceType(StrEnum):
    DOCX = "docx"
    WIKI = "wiki"


class RequirementRequest(BaseModel):
    source_url: str
    requester_open_id: str | None = None
    trigger_type: str = "local_link"
    reply_context: dict[str, str] = Field(default_factory=dict)


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
    ingest_issues: list[str] = Field(default_factory=list)
