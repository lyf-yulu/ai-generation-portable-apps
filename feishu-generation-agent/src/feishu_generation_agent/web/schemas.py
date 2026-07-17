from typing import Literal
from urllib.parse import quote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from feishu_generation_agent.domain.document import RequirementRequest
from feishu_generation_agent.domain.plan import (
    ApprovalDecision,
    GenerationTask,
    ImageReference,
)
from feishu_generation_agent.integrations.feishu_source import parse_feishu_url


class CreateRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_url: str = Field(min_length=1)

    @field_validator("source_url")
    @classmethod
    def validate_source_url(cls, value: str) -> str:
        normalized = value.strip()
        source_type, token = parse_feishu_url(normalized)
        hostname = urlsplit(normalized).hostname
        if hostname is None:
            raise ValueError("飞书文档链接缺少域名")
        return f"https://{hostname.lower()}/{source_type.value}/{quote(token, safe='')}"

    def to_domain(self) -> RequirementRequest:
        return RequirementRequest(source_url=self.source_url.strip())


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["approve", "reject", "cancel"]
    selected_task_ids: list[str] = Field(default_factory=list)
    tasks: list[GenerationTask] = Field(default_factory=list)
    feedback: str | None = None

    @model_validator(mode="after")
    def validate_action_fields(self) -> "DecisionRequest":
        if self.action == "approve":
            if not self.selected_task_ids:
                raise ValueError("批准时必须选择至少一个任务")
            if len(self.selected_task_ids) != len(set(self.selected_task_ids)):
                raise ValueError("不能重复选择同一任务")
            if self.feedback is not None:
                raise ValueError("批准时不能提交退回意见")
        elif self.action == "reject":
            if self.feedback is None or not self.feedback.strip():
                raise ValueError("退回重新规划时必须填写意见")
            if self.selected_task_ids or self.tasks:
                raise ValueError("退回时不能提交已选任务")
        elif self.selected_task_ids or self.tasks or self.feedback is not None:
            raise ValueError("取消时不能提交任务或意见")
        return self

    def to_domain(self) -> ApprovalDecision:
        return ApprovalDecision.model_validate(self.model_dump(mode="json"))


class ReferenceListRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    references: list[ImageReference] = Field(min_length=1)
