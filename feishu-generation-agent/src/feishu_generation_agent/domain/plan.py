from enum import StrEnum
import re
from typing import Any, Literal, Self

from pydantic import BaseModel, Field, field_validator, model_validator

from feishu_generation_agent.domain.image_prompt import (
    ImagePromptSlots,
    build_image_prompt,
    parse_prompt_slots,
)


class TaskType(StrEnum):
    IMAGE_TO_IMAGE = "image_to_image"
    IMAGE_TO_VIDEO = "image_to_video"


ReferenceMode = Literal["multi_reference", "first_last_frame"]
ImageProvider = Literal["seedream", "banana", "gpt-image2"]
DEFAULT_IMAGE_PROVIDER: ImageProvider = "banana"
# provider 只接受这三个基准分辨率档位；像素尺寸属于 size_variants。
IMAGE_SIZE_TOKENS = ("1K", "1.5K", "2K")
# 反解只取标签段，标签外的 @图片N 会丢；拼装后要按这个把它们补回去。
_REFERENCE_TOKEN = re.compile(r"@(?:图片|视频|音频)\d+")
# 统一按最高档出图，再裁到交付尺寸，避免小档位放大导致画质损失。
DEFAULT_IMAGE_SIZE = "2K"


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
    # 模型只填槽位，最终 prompt 由代码按模板拼装——让模型自己写模板骨架
    # 实测不稳定（时而套用、时而退回视频三段式）。
    prompt_slots: ImagePromptSlots | None = None
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

    def _assemble_prompt_from_slots(self) -> None:
        """槽位齐备时用代码拼装的 prompt 覆盖模型自己写的那版。

        模型仍会写一版 prompt（schema 要求非空），但只要它给了槽位就以拼装
        结果为准：固定约束句必须一字不差，不能靠模型记得写。槽位缺失时保留
        模型原文，避免把任务弄成空 prompt。
        """
        # 模型常把槽位内容写进 prompt 文本而不填 prompt_slots（该字段不在
        # schema 的 required 里）。此时从带标签文本反解，而不是让任务失败——
        # 早先改成硬失败导致整个测试套件挂住，出图比不出图重要。
        original_prompt = self.prompt
        slots = self.prompt_slots or parse_prompt_slots(self.prompt)
        if slots is None:
            return
        if not slots.canvas.strip() and self.size_variants:
            # 画布尺寸没给就用交付尺寸兜底，避免模板缺这一句。
            slots = slots.model_copy(
                update={"canvas": self.size_variants[0].replace("x", "*")}
            )
        assembled = build_image_prompt(slots)
        if not assembled.strip():
            return
        # 每张挂载的参考图都必须在 prompt 里被引用，否则 validate_image_prompt
        # 判「缺少素材引用 @图片N」。两种缺失都要补：
        #   1. 模型把 token 写在标签段之外（反解只取标签段，会丢）
        #   2. 模型压根没为某些挂载图写引用（实测 4 张图只提 1 张）
        # token 序号按图片类参考图的挂载顺序算，与 reference_contract 一致。
        expected = [
            f"@图片{index}"
            for index, _ in enumerate(
                sorted(self.reference_images, key=lambda item: item.order),
                start=1,
            )
        ]
        # 原 prompt 里出现过的 token 也要算进来：模型可能引用了比 order 序号
        # 更靠后的图（例如把风格参考写成 @图片4、@图片5），这些同样不能丢。
        for token in _REFERENCE_TOKEN.findall(original_prompt):
            if token not in expected:
                expected.append(token)
        missing = [token for token in expected if token not in assembled]
        if missing:
            assembled = (
                f"{assembled}，画面风格严格参考 {'、'.join(missing)}"
            )
        self.prompt = assembled

    def _drop_safe_area_from_variants(self) -> None:
        """把误当成交付尺寸的安全区从 size_variants 里剔除。

        安全区是构图界限，不是交付物。契约已写明这点，但模型反复把它填进
        size_variants，导致多裁一张安全区尺寸的图、甚至按尺寸把同一个概念
        拆成多个任务。这是确定性规则，不该依赖模型听话。
        """
        if not self.safe_area or not self.size_variants:
            return
        normalized = (
            self.safe_area.strip().lower().replace("×", "x").replace("*", "x")
        )
        self.size_variants = [
            variant
            for variant in self.size_variants
            if variant.lower() != normalized
        ]

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
        # 统一按 2K 出图，再裁到交付尺寸：不做档位映射，避免小档位出图后
        # 放大导致画质损失。2K 是 provider 支持的最高档，裁切总有余量。
        self.image_size = DEFAULT_IMAGE_SIZE

    @model_validator(mode="after")
    def validate_type_specific_fields(self) -> Self:
        if self.task_type is TaskType.IMAGE_TO_IMAGE:
            if self.image_size is None:
                # 缺失时按最高档兜底，而不是让整个计划失败：出图分辨率由
                # 我们统一决定，交付尺寸靠 size_variants 裁切。
                self.image_size = DEFAULT_IMAGE_SIZE
            self._normalize_image_size()
            self._drop_safe_area_from_variants()
            self._assemble_prompt_from_slots()
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
