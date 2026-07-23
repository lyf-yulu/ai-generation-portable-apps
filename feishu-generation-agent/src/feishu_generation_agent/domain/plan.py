from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, Field, field_validator, model_validator


class TaskType(StrEnum):
    IMAGE_TO_IMAGE = "image_to_image"
    IMAGE_TO_VIDEO = "image_to_video"


ReferenceMode = Literal["multi_reference", "first_last_frame"]


class ImageReference(BaseModel):
    asset_id: str
    role: Literal[
        "reference_image",
        "first_frame",
        "last_frame",
        "reference_video",
        "reference_audio",
    ]
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
    reference_mode: ReferenceMode | None = None
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
            if self.reference_mode not in {None, "multi_reference"}:
                raise ValueError("image_to_image only supports multi_reference")
            if any(
                reference.role != "reference_image"
                for reference in self.reference_images
            ):
                raise ValueError("image_to_image only accepts reference_image")
            self.reference_mode = "multi_reference"
            return self

        if self.duration is None:
            raise ValueError("duration is required for image_to_video")
        if self.resolution is None:
            raise ValueError("resolution is required for image_to_video")
        if self.image_size is not None:
            raise ValueError("image_size is not allowed for image_to_video")
        self._normalize_video_reference_mode()
        return self

    def _normalize_video_reference_mode(self) -> None:
        references = sorted(self.reference_images, key=lambda item: item.order)
        roles = [reference.role for reference in references]
        is_exact_frame_pair = roles == ["first_frame", "last_frame"]
        if self.reference_mode == "first_last_frame":
            if not is_exact_frame_pair:
                raise ValueError(
                    "first_last_frame requires exactly one first_frame and one last_frame"
                )
            return
        if self.reference_mode == "multi_reference":
            if any(role in {"first_frame", "last_frame"} for role in roles):
                raise ValueError("multi_reference does not accept first_frame or last_frame")
            return
        if is_exact_frame_pair:
            self.reference_mode = "first_last_frame"
            return

        frame_orders = {
            reference.role: reference.order
            for reference in references
            if reference.role in {"first_frame", "last_frame"}
        }
        if frame_orders:
            constraints: list[str] = []
            first_order = frame_orders.get("first_frame")
            last_order = frame_orders.get("last_frame")
            if first_order is not None:
                constraints.append(f"第 {first_order} 张参考图定义开场状态")
            if last_order is not None:
                constraints.append(f"第 {last_order} 张参考图定义结尾状态")
            constraint = "；".join(constraints) + "。"
            if constraint not in self.prompt:
                self.prompt = f"{self.prompt}\n{constraint}"
            self.reference_images = [
                ImageReference(
                    asset_id=reference.asset_id,
                    role=(
                        "reference_image"
                        if reference.role in {"first_frame", "last_frame"}
                        else reference.role
                    ),
                    order=reference.order,
                )
                for reference in self.reference_images
            ]
        self.reference_mode = "multi_reference"


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
