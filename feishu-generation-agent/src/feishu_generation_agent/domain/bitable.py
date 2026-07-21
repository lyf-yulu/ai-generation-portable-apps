from enum import StrEnum

from pydantic import BaseModel, Field


class TableTaskStatus(StrEnum):
    PENDING = "待处理"
    PROCESSING = "处理中"
    WAITING_APPROVAL = "待审批"
    GENERATING = "生成中"
    WRITING_BACK = "回写中"
    COMPLETED = "已完成"
    FAILED = "失败"
    WRITEBACK_FAILED = "回写失败"


class BitableLocation(BaseModel):
    wiki_token: str
    app_token: str | None = None
    table_id: str
    view_id: str
    source_url: str


class BitableTaskSummary(BaseModel):
    record_id: str
    display_text: str
    source_url: str
    status: TableTaskStatus = TableTaskStatus.PENDING
    executor_open_ids: list[str] = Field(default_factory=list)
    has_result: bool = False


class BitableBinding(BaseModel):
    app_token: str
    table_id: str
    view_id: str
    record_id: str
    source_url: str
    display_text: str
    run_id: str
    thread_id: str
    claimant_open_id: str
    status: TableTaskStatus
    approval_version: int = Field(default=0, ge=0)
    plan_fingerprint: str | None = None
    reply_context: dict[str, str] = Field(default_factory=dict)
    last_error: str | None = None
