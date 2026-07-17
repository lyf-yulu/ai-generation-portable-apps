from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


class ProviderResult(BaseModel):
    url: str | None = None
    base64_data: str | None = None
    mime_type: str


class ProviderSubmission(BaseModel):
    provider: str
    provider_task_id: str
    status: str
    result_items: list[ProviderResult] = Field(default_factory=list)
    error_message: str | None = None


class ExecutionRecord(BaseModel):
    task_id: str
    provider: str
    provider_task_id: str | None = None
    status: str
    error: dict[str, object] | None = None


class Artifact(BaseModel):
    artifact_id: str
    task_id: str
    kind: Literal["image", "video"]
    local_path: Path
    mime_type: str
    size: int
    sha256: str
    provider_url: str | None = None
    provider_task_id: str | None = None
    feishu_file_token: str | None = None
    status: str


class DeliveryRecord(BaseModel):
    document_id: str
    document_url: str
    status: str
    uploaded_artifact_ids: list[str] = Field(default_factory=list)
