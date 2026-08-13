from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import BaseModel, Field, field_validator, model_validator


class TaskType(StrEnum):
    IMAGE_TO_IMAGE = "image_to_image"
    IMAGE_TO_VIDEO = "image_to_video"


ReferenceMode = Literal["multi_reference", "first_last_frame"]
ImageProvider = Literal["seedream", "banana", "gpt-image2"]
DEFAULT_IMAGE_PROVIDER: ImageProvider = "banana"
# provider 只接受这三个基准分辨率档位；像素尺寸属于 size_variants。
IMAGE_SIZE_TOKENS = ("1K", "1.5K", "2K")


def _contains_chinese(value: str) -> bool:
    return any(
        "\u3400" <= character <= "\u4dbf"
        or "\u4e00" <= character <= "\u9fff"
        for character in value
    )


class ExcludedAsset(BaseModel):
    asset_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)

    @field_validator("reason")
    @classmethod
    def require_chinese_reason(cls, value: str) -> str:
        if not _contains_chinese(value):
            raise ValueError("排除理由必须包含中文")
        return value


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
    image_provider: ImageProvider | None = None
    size_variants: list[str] = Field(default_factory=list)
    safe_area: str | None = None
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

    @property
    def resolved_image_provider(self) -> ImageProvider | None:
        """图片任务的实际 provider；视频任务返回 None。

        不在校验器里回填 image_provider，避免 model_dump() 携带隐式字段后
        被复用去构造视频任务时撞上「video 不允许 image_provider」的护栏。
        """
        if self.task_type is not TaskType.IMAGE_TO_IMAGE:
            return None
        return self.image_provider or DEFAULT_IMAGE_PROVIDER

    @property
    def resolved_size_variants(self) -> list[str]:
        """图片任务要产出的尺寸变体；未显式指定时回退到 image_size 单尺寸。"""
        if self.task_type is not TaskType.IMAGE_TO_IMAGE:
            return []
        return list(self.size_variants)

    @field_validator("size_variants")
    @classmethod
    def normalize_size_variants(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for item in value:
            candidate = (
                item.strip().lower().replace("×", "x").replace("*", "x")
            )
            if not candidate:
                continue
            width, separator, height = candidate.partition("x")
            if (
                not separator
                or not width.isdigit()
                or not height.isdigit()
                or int(width) <= 0
                or int(height) <= 0
            ):
                raise ValueError(
                    f"size_variants 需要形如 1700x2500 的尺寸，收到 {item!r}"
                )
            canonical = f"{int(width)}x{int(height)}"
            if canonical not in normalized:
                normalized.append(canonical)
        return normalized

    def _normalize_image_size(self) -> None:
        """把误填进 image_size 的像素尺寸搬到 size_variants。

        需求文档原文写的是「尺寸：1700*2500」，planner 很自然会把它填进
        image_size。但 provider 只认 1K/1.5K/2K 三个基准档位，像素尺寸传
        过去会直接被拒；而 size_variants 空着又会让出图后的裁切不触发。
        所以在领域层归一化，而不是只靠契约措辞约束。
        """
        raw = (self.image_size or "").strip()
        if raw.upper() in {token.upper() for token in IMAGE_SIZE_TOKENS}:
            self.image_size = next(
                token
                for token in IMAGE_SIZE_TOKENS
                if token.upper() == raw.upper()
            )
            return

        candidate = raw.lower().replace("×", "x").replace("*", "x")
        width_text, separator, height_text = candidate.partition("x")
        if (
            not separator
            or not width_text.isdigit()
            or not height_text.isdigit()
        ):
            raise ValueError(
                "image_size 只能是 1K、1.5K、2K，"
                f"像素尺寸请写入 size_variants，收到 {self.image_size!r}"
            )
        width = int(width_text)
        height = int(height_text)
        if width <= 0 or height <= 0:
            raise ValueError(f"image_size 尺寸无效：{self.image_size!r}")

        pixel_variant = f"{width}x{height}"
        if pixel_variant not in self.size_variants:
            self.size_variants = [*self.size_variants, pixel_variant]
        # 按长边归到最近的基准档位，保证 provider 出图分辨率不低于交付尺寸。
        longest = max(width, height)
        if longest <= 1024:
            self.image_size = "1K"
        elif longest <= 1600:
            self.image_size = "1.5K"
        else:
            self.image_size = "2K"

    @model_validator(mode="after")
    def validate_type_specific_fields(self) -> Self:
        if self.task_type is TaskType.IMAGE_TO_IMAGE:
            if self.image_size is None:
                raise ValueError("image_size is required for image_to_image")
            self._normalize_image_size()
            for field_name in ("duration", "resolution", "generate_audio"):
                if getattr(self, field_name) is not None:
                    raise ValueError(
                        f"{field_name} is not allowed for image_to_image"
                    )
            if self.reference_mode not in {None, "multi_reference"}:
                raise ValueError("image_to_image only supports multi_reference")
            self.reference_mode = "multi_reference"
            return self

        if self.duration is None:
            raise ValueError("duration is required for image_to_video")
        if self.resolution is None:
            raise ValueError("resolution is required for image_to_video")
        if self.image_size is not None:
            raise ValueError("image_size is not allowed for image_to_video")
        if self.image_provider is not None:
            raise ValueError("image_provider is not allowed for image_to_video")
        if self.size_variants:
            raise ValueError("size_variants is not allowed for image_to_video")
        if self.safe_area is not None:
            raise ValueError("safe_area is not allowed for image_to_video")
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
    excluded_assets: list[ExcludedAsset]

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_missing_exclusions(cls, value: Any) -> Any:
        if isinstance(value, dict) and "excluded_assets" not in value:
            return {**value, "excluded_assets": []}
        return value

    @model_validator(mode="after")
    def validate_plan_identity_sets(self) -> Self:
        task_ids = [task.task_id for task in self.tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("duplicate task_id")
        excluded_ids = [item.asset_id for item in self.excluded_assets]
        if len(excluded_ids) != len(set(excluded_ids)):
            raise ValueError("duplicate excluded asset_id")
        referenced_ids = {
            reference.asset_id
            for task in self.tasks
            for reference in task.reference_images
        }
        overlap = referenced_ids.intersection(excluded_ids)
        if overlap:
            raise ValueError(
                "referenced and excluded asset sets overlap: "
                + ", ".join(sorted(overlap))
            )
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

        selected_references = {
            reference.asset_id
            for task in selected_tasks
            for reference in task.reference_images
        }
        unselected_references = {
            reference.asset_id
            for task in self.tasks
            if task.task_id not in selected_id_set
            for reference in task.reference_images
        }
        exclusions = list(self.excluded_assets)
        excluded_ids = {item.asset_id for item in exclusions}
        for asset_id in sorted(unselected_references - selected_references):
            if asset_id not in excluded_ids:
                exclusions.append(
                    ExcludedAsset(
                        asset_id=asset_id,
                        reason="用户未选择对应任务，因此本次不使用该素材。",
                    )
                )

        return TaskPlan(
            tasks=selected_tasks,
            document_summary=self.document_summary,
            excluded_assets=exclusions,
        )


def reconcile_asset_coverage(
    plan: TaskPlan,
    *,
    added_asset_ids: set[str] = frozenset(),
    removed_asset_ids: set[str] = frozenset(),
) -> TaskPlan:
    referenced_ids = {
        reference.asset_id
        for task in plan.tasks
        for reference in task.reference_images
    }
    exclusions = [
        item
        for item in plan.excluded_assets
        if item.asset_id not in referenced_ids
        and item.asset_id not in added_asset_ids
    ]
    excluded_ids = {item.asset_id for item in exclusions}
    for asset_id in sorted(removed_asset_ids - referenced_ids):
        if asset_id not in excluded_ids:
            exclusions.append(
                ExcludedAsset(
                    asset_id=asset_id,
                    reason="用户在审批中移除",
                )
            )
    return TaskPlan(
        tasks=plan.tasks,
        document_summary=plan.document_summary,
        excluded_assets=exclusions,
    )


def reconcile_task_asset_coverage(
    plan: TaskPlan,
    tasks: list[GenerationTask],
) -> TaskPlan:
    previous_ids = {
        reference.asset_id
        for task in plan.tasks
        for reference in task.reference_images
    }
    updated_ids = {
        reference.asset_id
        for task in tasks
        for reference in task.reference_images
    }
    candidate = TaskPlan(
        tasks=tasks,
        document_summary=plan.document_summary,
        excluded_assets=[
            item
            for item in plan.excluded_assets
            if item.asset_id not in updated_ids
        ],
    )
    return reconcile_asset_coverage(
        candidate,
        added_asset_ids=updated_ids - previous_ids,
        removed_asset_ids=previous_ids - updated_ids,
    )


class AuditReport(BaseModel):
    issues: list[str] = Field(default_factory=list)
    corrections_required: bool = False


class ApprovalDecision(BaseModel):
    action: Literal["approve", "reject", "cancel"]
    selected_task_ids: list[str] = Field(default_factory=list)
    tasks: list[GenerationTask] = Field(default_factory=list)
    feedback: str | None = None
