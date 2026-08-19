"""按需求方给定的模板拼装图片 prompt。

为什么由代码拼而不是让模型写：需求方那套约束句（禁止勾勒边缘线、禁止拉伸
图片、禁止出现窗户、高亮度高明度…）是踩坑总结出来的，必须一字不差。实测
让模型照契约写，四轮下来时而套用模板、时而退回视频三段式——契约本身已验证
正确且确实送到模型面前，问题是遵守不稳定。

改成模型只填槽位、固定句式由这里拼出来，模型就没有跑偏空间。
"""

from pydantic import BaseModel, Field

# 模型没填 prompt_slots 时，从它自己写的带标签文本里反解。实测它会写成
# 「景别：中景；时间与场景：…；动作：…」这种结构——解析它比强迫它改格式稳。
_SLOT_LABELS: dict[str, tuple[str, ...]] = {
    "shot": ("景别",),
    "time_and_scene": ("时间与场景", "时间和场景"),
    "subject_integration": ("主体融合", "画面内容", "主体"),
    "action": ("动作", "人物活动"),
    "background": ("背景",),
    "style": ("风格", "画风"),
    "canvas": ("画布", "画布尺寸"),
    "mood": ("氛围", "整体氛围"),
    "time_of_day": ("时间",),
}
_SEGMENT_SEPARATORS = "；;\n"


class ImagePromptSlots(BaseModel):
    """模板里需要模型填的槽位。

    每项都对应需求文档里的一处内容，模型只负责提取，不负责组织句式。
    """

    shot: str = Field(default="", description="景别，如中景/近景/全景")
    time_and_scene: str = Field(default="", description="时间与场景")
    subject_integration: str = Field(
        default="", description="人物如何自然设身处地融入场景"
    )
    action: str = Field(default="", description="画面中人物的活动")
    background: str = Field(default="", description="背景")
    style: str = Field(default="", description="画风提示词")
    canvas: str = Field(default="", description="画布尺寸，如 1700*2500")
    mood: str = Field(default="", description="整体氛围")
    time_of_day: str = Field(default="白天", description="末尾的时间")


def parse_prompt_slots(prompt: str) -> ImagePromptSlots | None:
    """从模型写的带标签文本里反解槽位；解析不出来返回 None。

    模型不填 prompt_slots 时会把内容写成「景别：中景；时间与场景：…」这种
    结构。解析它比强迫模型改输出格式稳定得多。解析失败返回 None，由调用方
    保留原文——出图比不出图重要，不能因为格式不合就阻断任务。
    """
    if not prompt or not prompt.strip():
        return None

    # 先按分号/换行切段，再在段内找「标签：值」。
    segments: list[str] = []
    for raw in prompt.replace("\r", "").split("\n"):
        for part in raw.split("；"):
            for piece in part.split(";"):
                if piece.strip():
                    segments.append(piece.strip())

    found: dict[str, str] = {}
    for segment in segments:
        for field, labels in _SLOT_LABELS.items():
            if field in found:
                continue
            for label in labels:
                for marker in (f"{label}：", f"{label}:"):
                    index = segment.find(marker)
                    if index < 0:
                        continue
                    value = segment[index + len(marker):].strip().rstrip("。")
                    if value:
                        found[field] = value
                    break
                if field in found:
                    break

    # 只认出「时间」这类通用标签时不算解析成功，避免拼出空壳 prompt。
    meaningful = {
        key for key in found if key not in {"time_of_day", "canvas"}
    }
    if not meaningful:
        return None
    return ImagePromptSlots.model_validate(found)


def build_image_prompt(slots: ImagePromptSlots) -> str:
    """把槽位拼成完整 prompt。

    固定句式与顺序都不可变。空槽位整段跳过，避免出现「整体氛围，」这类
    悬空标点。
    """
    segments: list[str] = ["禁止勾勒边缘线", "画面风格严格参考图一重新生成图片"]

    for value in (slots.shot, slots.time_and_scene):
        if value.strip():
            segments.append(value.strip())

    if slots.subject_integration.strip():
        segments.append(f"画面内容参考{slots.subject_integration.strip()}")
    if slots.action.strip():
        segments.append(slots.action.strip())
    if slots.background.strip():
        segments.append(f"背景是{slots.background.strip()}")

    # 「画面主次分明」起新句，与前半段的画面描述分开。
    head = "，".join(segments) + "。"

    tail: list[str] = ["画面主次分明"]
    if slots.style.strip():
        tail.append(slots.style.strip())
    body = "，".join(tail) + "。"

    closing: list[str] = []
    if slots.canvas.strip():
        closing.append(f"画面主体控制在画布{slots.canvas.strip()}画面中央")
    closing.extend(["光影自然", "冷暖对比", "光线暖柔"])
    if slots.mood.strip():
        closing.append(f"整体氛围{slots.mood.strip()}")
    closing.extend(
        [
            "整体画面高亮度高明度",
            "画面风格严格参考图一生成图片",
            "禁止勾勒边缘线",
            "禁止勾勒边缘线",
            "禁止拉伸图片",
        ]
    )
    if slots.time_of_day.strip():
        closing.append(slots.time_of_day.strip())
    # 反 AI 味约束：模型很爱堆窗户，堆多了一眼看出是 AI 生成，始终保留。
    closing.append("禁止出现窗户")

    return f"{head}{body}{'，'.join(closing)}"
