from pydantic import BaseModel, Field, computed_field

from feishu_generation_agent.domain.bitable import BitableLocation, TableTaskStatus


class ProductionSchema(BaseModel):
    requirement_name_field_id: str
    requirement_attachment_field_id: str
    project_name_field_id: str
    requester_field_id: str
    maker_field_id: str
    progress_field_id: str


class ProductionSourceSnapshot(BaseModel):
    requirement_name: str
    requirement_attachment: str
    project_names: list[str] = Field(default_factory=list)
    requester_open_ids: list[str] = Field(default_factory=list)
    requester_names: list[str] = Field(default_factory=list)
    maker_open_ids: list[str] = Field(default_factory=list)
    maker_names: list[str] = Field(default_factory=list)


class ProductionTaskSummary(BaseModel):
    record_id: str
    display_text: str
    source_url: str
    progress: str
    maker_open_id: str | None = None
    maker_name: str | None = None
    snapshot: ProductionSourceSnapshot

    @computed_field
    @property
    def deliverable(self) -> bool:
        return self.maker_open_id is not None

    @computed_field
    @property
    def delivery_block_reason(self) -> str | None:
        return None if self.deliverable else "缺少需求制作人"


class ProductionBinding(BaseModel):
    source_location: BitableLocation
    record_id: str
    source_url: str
    display_text: str
    progress: str
    maker_open_id: str | None = None
    maker_name: str | None = None
    snapshot: ProductionSourceSnapshot
    run_id: str
    thread_id: str
    status: TableTaskStatus
    last_error: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class ResultTableTarget(BaseModel):
    maker_open_id: str
    maker_name: str
    app_token: str
    table_id: str
    url: str


class ProductionDelivery(BaseModel):
    run_id: str
    result_record_id: str | None = None
    status: str
