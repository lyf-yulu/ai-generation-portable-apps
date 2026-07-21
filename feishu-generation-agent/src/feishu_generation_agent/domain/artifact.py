from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class ProviderResult(BaseModel):
    url: str | None = None
    url_trust: Literal["untrusted"] | None = None
    base64_data: str | None = None
    local_path: Path | None = None
    mime_type: str
    size: int | None = None
    sha256: str | None = None

    @model_validator(mode="after")
    def validate_source_boundary(self) -> "ProviderResult":
        sources = [
            self.url is not None,
            self.base64_data is not None,
            self.local_path is not None,
        ]
        if sum(sources) != 1:
            raise ValueError("provider result must have exactly one result source")
        if self.url is not None:
            if self.url_trust != "untrusted":
                raise ValueError("url_trust must mark provider URLs as untrusted")
            if self.size is not None or self.sha256 is not None:
                raise ValueError("untrusted URL cannot claim local integrity metadata")
        elif self.url_trust is not None:
            raise ValueError("url_trust is only valid for URL results")
        if self.local_path is not None:
            if self.size is None or self.size <= 0:
                raise ValueError("local provider result requires positive size")
            if (
                not isinstance(self.sha256, str)
                or len(self.sha256) != 64
                or any(character not in "0123456789abcdef" for character in self.sha256)
            ):
                raise ValueError("local provider result requires lowercase sha256")
        elif self.size is not None or self.sha256 is not None:
            raise ValueError("integrity metadata requires local_path")
        return self


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
    status: str
    target_type: Literal["docx", "bitable_record"] = "docx"
    document_id: str | None = None
    document_url: str | None = None
    app_token: str | None = None
    table_id: str | None = None
    record_id: str | None = None
    uploaded_artifact_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_delivery_target(self) -> "DeliveryRecord":
        if self.target_type == "docx":
            if not self.document_id or not self.document_url:
                raise ValueError("docx delivery requires document identity")
        elif not self.app_token or not self.table_id or not self.record_id:
            raise ValueError("bitable delivery requires app/table/record identity")
        return self
