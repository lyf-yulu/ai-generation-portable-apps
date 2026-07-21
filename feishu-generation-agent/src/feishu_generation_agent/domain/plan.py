from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, Field, field_validator, model_validator


class TaskType(StrEnum):
    IMAGE_TO_IMAGE = "image_to_image"
    IMAGE_TO_VIDEO = "image_to_video"


class ImageReference(BaseModel):
    asset_id: str
    role: Literal["reference_image", "first_frame", "last_frame"]
    order: int = Field(ge=1)

    @field_validator("role", mode="before")
    @classmethod
    def normalize_saved_planner_role(cls, value: object) -> object:
        if value == "character_and_style_reference":
            return "reference_image"
        return value


class GenerationTask(BaseModel):
    task_id: str
    task_type: TaskType
    title: str
    source_block_ids: list[str]
    user_intent: str
    prompt: str
    negative_constraints: list[str] = Field(default_factory=list)
    reference_images: list[ImageReference] = Field(min_length=1)
    aspect_ratio: str
    image_size: str | None = None
    duration: int | None = None
    resolution: Literal["720p", "1080p"] | None = None
    generate_audio: bool | None = None
    output_count: int = Field(default=1, ge=1)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    assumptions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    blocking_issues: list[str] = Field(default_factory=list)

    @field_validator("resolution", mode="before")
    @classmethod
    def normalize_video_resolution(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        normalized = value.strip().lower().replace("×", "x")
        aliases = {
            "720x1280": "720p",
            "1280x720": "720p",
            "1080x1920": "1080p",
            "1920x1080": "1080p",
        }
        return aliases.get(normalized, normalized)

    @model_validator(mode="after")
    def validate_type_specific_fields(self) -> Self:
        if self.task_type is TaskType.IMAGE_TO_IMAGE:
            if self.image_size is None:
                raise ValueError("image_size is required for image_to_image")
            for field_name in ("duration", "resolution", "generate_audio"):
                if getattr(self, field_name) is not None:
                    raise ValueError(
                        f"{field_name} is not allowed for image_to_image"
                    )
            return self

        if self.duration is None:
            raise ValueError("duration is required for image_to_video")
        if self.resolution is None:
            raise ValueError("resolution is required for image_to_video")
        if self.image_size is not None:
            raise ValueError("image_size is not allowed for image_to_video")
        return self


class TaskPlan(BaseModel):
    tasks: list[GenerationTask]
    document_summary: str = ""

    @model_validator(mode="after")
    def reject_duplicate_task_ids(self) -> Self:
        task_ids = [task.task_id for task in self.tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("duplicate task_id")
        return self

    def approved_subset(
        self,
        selected_ids: list[str],
        max_output_count: int,
    ) -> "TaskPlan":
        if len(selected_ids) != len(set(selected_ids)):
            raise ValueError("duplicate selected task_id")

        known_ids = {task.task_id for task in self.tasks}
        unknown_ids = set(selected_ids) - known_ids
        if unknown_ids:
            unknown = ", ".join(sorted(unknown_ids))
            raise ValueError(f"unknown selected task_id: {unknown}")

        selected_id_set = set(selected_ids)
        selected_tasks = [
            task for task in self.tasks if task.task_id in selected_id_set
        ]
        for task in selected_tasks:
            if task.blocking_issues:
                raise ValueError(
                    f"task {task.task_id} has blocking issues and cannot be approved"
                )
            if task.output_count > max_output_count:
                raise ValueError(
                    f"task {task.task_id} output_count exceeds max_output_count"
                )

        return TaskPlan(
            tasks=selected_tasks,
            document_summary=self.document_summary,
        )


class AuditReport(BaseModel):
    issues: list[str] = Field(default_factory=list)
    corrections_required: bool = False


class ApprovalDecision(BaseModel):
    action: Literal["approve", "reject", "cancel"]
    selected_task_ids: list[str] = Field(default_factory=list)
    tasks: list[GenerationTask] = Field(default_factory=list)
    feedback: str | None = None
