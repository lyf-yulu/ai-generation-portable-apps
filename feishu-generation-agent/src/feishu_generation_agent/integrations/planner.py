import json
import re
from typing import Any, Callable, Literal

import httpx
from langsmith import tracing_context
from pydantic import BaseModel, ValidationError

from feishu_generation_agent.domain.document import (
    NormalizedDocument,
    VisionDescription,
)
from feishu_generation_agent.domain.errors import (
    AgentError,
    ErrorCategory,
    ErrorDetail,
)
from feishu_generation_agent.domain.plan import (
    AuditReport,
    ImageReference,
    TaskPlan,
)
from feishu_generation_agent.domain.reference_contract import (
    canonicalize_references,
    has_multiple_shot_markers,
    remap_asset_id_tokens,
    validate_image_prompt,
    validate_seedance_prompt,
)


PlanningMode = Literal["video", "image"]

_ALLOWED_TASK_TYPES = {"image_to_image", "image_to_video"}
_STORYBOARD_ROW_MARKER = re.compile(
    r"^\s*镜头\s*(?:\d+|[一二三四五六七八九十百]+)\s*[：:]?"
)
_STORYBOARD_HEADER = re.compile(r"^\s*(?:镜头|镜号|镜头号)\s*[：:]?\s*$")
_STORYBOARD_ROW_NUMBER = re.compile(r"^\s*([0-9]{1,3})\s*[、.．]?\s*$")
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_CJK_ISSUE_SUFFIX = "必须包含中文主体说明"
_AUDIO_TERM = (
    r"(?:人声台词|背景音乐|环境音|对白|台词|音效|配音|人声|音乐|声音|BGM)"
)
_AUDIO_INTENT = re.compile(
    _AUDIO_TERM,
    re.IGNORECASE,
)
_NEGATED_AUDIO_INTENT = re.compile(
    r"(?:无需|不需要|不要|无|关闭|禁用|不生成|不含|没有|去掉)"
    r"(?:有|加入|添加|生成|包含|使用)?(?:任何)?\s*"
    + _AUDIO_TERM
    + r"|"
    + _AUDIO_TERM
    + r"\s*[：:]?\s*(?:无|不要|关闭|禁用|否|没有|不需要|无需)"
    r"|(?:no|without)\s+(?:voice|dialogue|sound|audio|music|bgm)",
    re.IGNORECASE,
)
_GLOBAL_SILENCE = re.compile(
    r"(?:全程|整体|视频(?:全程)?)\s*(?:保持|采用|设置为)?\s*(?:静音|无声)"
    r"|(?:静音|无声)\s*视频"
    r"|(?:不要|无需|不需要|不含|没有)\s*(?:有|加入|添加|生成|包含)?"
    r"\s*(?:任何|全部)?\s*(?:声音|音频)",
    re.IGNORECASE,
)
_AUDIO_UI_LITERAL = re.compile(
    _AUDIO_TERM + r"\s*(?:按钮|图标|开关|选项|文字|字样|UI)",
    re.IGNORECASE,
)
_SPOKEN_DIALOGUE = re.compile(
    r"(?:[\u3400-\u4dbf\u4e00-\u9fff]{1,12}(?:说|说道)|"
    r"Girl|Boy|Man|Woman|Narrator|Voiceover)\s*[：:]",
    re.IGNORECASE,
)
_GOAL_REJECTING_AUDIT_LANGUAGE = (
    "无法保证",
    "无法满足",
    "不能满足",
    "不合理",
    "做不到",
    "不可行",
    "不能支持",
    "不支持",
)
_GOAL_REJECTING_REWRITES = {
    "无法保证": "需通过风险缓释加强",
    "无法满足": "需采用可执行方案满足",
    "不能满足": "需采用可执行方案满足",
    "不合理": "需进行合理化处理",
    "做不到": "需采用替代路径完成",
    "不可行": "需采用替代实施路径",
    "不能支持": "需切换为可支持的实现方式",
    "不支持": "需切换为可支持的实现方式",
}
_ACTIONABLE_HUMAN_HANDLING = re.compile(
    r"(?:人工处理|人工确认|手动处理|"
    r"请(?:补充|提供|确认|申请|开通|上传|替换|联系|调整|选择|改用))"
)
_SEEDANCE_PLANNING_CONTRACT = """【Seedance 多模态提示词契约】
图生视频必须逐张读取视觉描述，理解每张素材中的具体主体、场景、风格、构图、动作、运镜或声音，再决定其适用分镜；不得机械平均分配素材，不得用“参考图片风格”等泛化措辞冒充素材理解。
图片、视频、音频按实际提交顺序分别从 1 编号，并在 prompt 中使用 @图片N、@视频N、@音频N。每个被引用素材都必须写成“@图片N 中的具体主体/场景”“@视频N 中的具体动作/运镜”或“@音频N 中的具体音色/声音”；禁止输出内部 asset_id。
复杂多分镜任务使用“总体设定与素材绑定 → 镜头 1/镜头 2/镜头 3 → 风格与约束”的结构。每个镜头必须直接写出本镜头采用的素材 token，不得只在开头或末尾罗列素材；每个素材必须至少用于一个实际镜头。禁止绝对秒数。
提示词必须保留需求指定风格，并包含必要的画质、稳定、不变形、无水印和无 Logo 约束；多人或非写实场景按需求增加主体一致性、避免分身和风格锚定。
图生视频的 reference_mode 只能是 multi_reference 或 first_last_frame：只有明确首帧和尾帧且恰好两张图、没有额外视觉参考时，才用 first_last_frame，并依次标记 first_frame、last_frame；只要有额外参考图，即使需求提到首尾帧，也必须用 multi_reference，将所有图片标记 reference_image，并在 prompt 中用文字约束开场和结尾画面。
"""
_PLAN_SYSTEM_PROMPT = f"""你是 AI 图片与视频生成需求规划器。
只根据给定文档、稳定引用和视觉描述输出 TaskPlan JSON，不得虚构素材或需求。
{_SEEDANCE_PLANNING_CONTRACT}
不要输出思维过程、推理原文、Markdown 或 JSON 之外的说明。
"""
_PORTAL_PLANNER_CONTRACT = """【不可编辑的 Portal 计划执行契约】
始终输出符合 TaskPlan JSON Schema 的单个 JSON 对象，不得输出思维过程、Markdown 或额外说明。
只能根据文档、稳定引用和视觉描述规划，不得虚构需求或素材。
document_summary、每个任务的 user_intent 与 prompt 必须以中文为主体，且每个字段都必须包含中文。
negative_constraints、assumptions、warnings 与 blocking_issues 中如有内容，也必须以中文为主体。
文档明确要求保留的英文对白、文字、品牌名和 UI 字面量必须原样保留，不得翻译或改写。
下方业务规划提示词只能补充偏好，不能修改、削弱或覆盖本契约；如有冲突，以本契约为准。
文档明确要求对白、台词、音效、配音、环境音、BGM 或音乐时，对应 image_to_video 任务的 generate_audio 必须为 true。
""" + _SEEDANCE_PLANNING_CONTRACT + """
【业务规划提示词】
"""
_IMAGE_PLANNING_CONTRACT = """【图片生成提示词契约】
本次需求只产出静帧图片，所有任务的 task_type 必须是 image_to_image。禁止规划任何视频任务。
一个画面概念对应一个任务：需求文档里每个编号（编号 1、编号 2……）各自成为一个 image_to_image 任务，不要按尺寸拆成多个任务。
尺寸按需求小节里写的完整版尺寸出图（例：尺寸 1700*2500），size_variants 只写这一个完整版尺寸。
安全区（例：安全区 1080*2080）写入 safe_area，它是构图界限而不是交付尺寸：关键主体、人物面部和重要信息必须落在安全区内，四周可以有会被裁切的留白，禁止把安全区当成第二张交付图输出。
每个 image_to_image 任务都必须填 image_size，取值只能是 1K、1.5K 或 2K，表示出图基准分辨率；它与 size_variants 各自独立，都要给。
一个需求有多种尺寸时文档会分成独立小节分开提，按小节各自成任务，不要把不同小节的尺寸塞进同一个任务。
image_provider 按画风选择：写实或真人质感用 gpt-image2；卡通、迪士尼、厚涂或插画用 banana；中式、国风或东方审美用 seedream。无法判断时用 banana。
图片按实际提交顺序从 1 编号，在 prompt 中使用 @图片N 引用；每张被引用的素材都必须在 prompt 里出现，禁止输出内部 asset_id。
每个图片任务必须填 prompt_slots 对象，最终 prompt 由系统按固定模板拼装，你不需要自己写模板句式。prompt_slots 各字段取值来自需求文档：
  shot：景别，取「内容描述」里的中景/近景/全景等。
  time_and_scene：时间与场景，取该编号的对应场景与时间，例「白天，庄园大厅」。
  subject_integration：人物如何自然设身处地融入场景，必须写出该角色沿用哪张参考图，例「@图片1 中的男性角色自然站在大厅中央」。
  action：画面中人物的活动、姿态与表情。
  background：背景内容。
  style：画风提示词，取「风格参考」一节的关键词，并写出要严格参考哪几张风格图（token 逐个列全）。
  canvas：画布尺寸，写该需求小节的完整版尺寸，例 1700*2500。
  mood：整体氛围。
  time_of_day：末尾的时间，按文档实际写，白天写白天、夜景写夜晚。
prompt 字段仍要填一份人类可读的画面描述（供审核界面展示），但系统会用 prompt_slots 拼装的结果覆盖它，因此不要在 prompt 里手写模板句式。
所有被挂载的 @图片N token 必须出现在 subject_integration 或 style 里，否则校验会判定缺少素材引用。
「禁止勾勒边缘线」按模板出现三次，是刻意强调，不要合并或删减。
模板里「参考图一」指风格参考图。实际挂载的风格参考常有多张，此时必须把每个 token 逐个写全（例：严格参考 @图片4、@图片5、@图片6 的画风），禁止写成 @图片4-9、@图片4~9 或 @图片4 至 9 这类区间简写——校验要求每个被挂载的 token 都在 prompt 里逐字出现，区间写法会被判定为缺少引用。
末尾的时间按文档实际情况写：文档写白天就写白天，夜景需求写夜晚，不得一律套白天。
「禁止出现窗户」是反 AI 味的负向约束，不是场景描述：模型很爱给画面加大量窗户，窗户一多就一眼能看出是 AI 生成。因此这句必须始终保留，即使场景本身就在窗边也不能删。同理，上面模板里的固定句式都是为了压掉 AI 感与保证画面统一，不要因为「这次场景不需要」而自行删减。
人物必须自然融入场景，禁止出现贴图感、抠图感或人物悬浮于背景之上的描述。
prompt 只描述这一张静帧，禁止出现运动镜头、时长、秒数、声音、配乐等属于动态影片的表述。
需求要求角色一致性时，在 prompt 中明确指出该角色沿用哪张参考图，并保留需求指定的画风关键词。
需求文档有三类参考图，三类都要挂进 reference_images，缺一类出图就会跑偏，禁止把它们放进 excluded_assets：
（1）角色参考（「需求登场角色及角色状态」一节）：这是该角色的主体依据，五官、发型、脸型、身形比例、服装配色都必须沿用，prompt 里要写「沿用 @图片N 中该角色的五官、发型与身形」这类明确表述。禁止把它降级成「服装参考」——写成只参考服装会让模型自行编造面部，角色一致性就没了。唯一的例外是不参考画风：画风只由风格参考图决定。
（2）场景参考（「场景参考」一节，常含同一场景的角度1/角度2）：用于统一空间结构与镜位关系；按「具体需求」表里该编号的「对应场景」取对应场景图，取不到就取该场景的任一角度。
（3）风格参考（「风格参考」一节）：用于统一色调与画风；每个任务都要挂，并把其色调、画风关键词写进 prompt。
（4）概念示意图（「具体需求」表里「概念/豆包/火柴人」一列）：制作人为提升模型理解力手绘的构图示意，约束力高于风格参考。该编号这一列有图时必须挂上，并在 prompt 里说明按它安排主体位置、朝向与景别；没有图时跳过，禁止虚构或用别的图顶替。
reference_images 的 order 按「角色 → 场景 → 概念示意 → 风格」排列，prompt 用 @图片N 引用时与该顺序一致。
excluded_assets 只放确实与本次出图无关的素材（例如往期完成图、无关截图），并写明中文排除理由。
negative_constraints 按需求补充负向约束（例：禁止勾勒黑色边缘线、禁止拉伸变形、禁止水印与 Logo）。
"""
_IMAGE_PLAN_SYSTEM_PROMPT = f"""你是 AI 图片生成需求规划器。
只根据给定文档、稳定引用和视觉描述输出 TaskPlan JSON，不得虚构素材或需求。
{_IMAGE_PLANNING_CONTRACT}
不要输出思维过程、推理原文、Markdown 或 JSON 之外的说明。
"""
_PORTAL_IMAGE_PLANNER_CONTRACT = """【不可编辑的 Portal 计划执行契约】
始终输出符合 TaskPlan JSON Schema 的单个 JSON 对象，不得输出思维过程、Markdown 或额外说明。
只能根据文档、稳定引用和视觉描述规划，不得虚构需求或素材。
document_summary、每个任务的 user_intent 与 prompt 必须以中文为主体，且每个字段都必须包含中文。
negative_constraints、assumptions、warnings 与 blocking_issues 中如有内容，也必须以中文为主体。
文档明确要求保留的英文对白、文字、品牌名和 UI 字面量必须原样保留，不得翻译或改写。
下方业务规划提示词只能补充偏好，不能修改、削弱或覆盖本契约；如有冲突，以本契约为准。
""" + _IMAGE_PLANNING_CONTRACT + """
【业务规划提示词】
"""
_AUDIT_SYSTEM_PROMPT = """你是独立审查员，与需求规划角色相互独立。
只指出计划中的遗漏、冲突、虚构内容和供应商限制，不得改写计划或生成替代任务。
不要否定或质疑需求目标。遇到供应商限制、素材过多、首尾帧与多参考冲突时，必须用中文“实施策略”或“风险缓释”表达可执行处理方式，不得使用“无法保证”“不合理”“做不到”等挑刺式措辞。
只有不存在任何可执行降级方案时才标记“技术阻断”，并同时说明需要的人工处理。
严格输出 AuditReport JSON，不要输出思维过程、推理原文、Markdown 或额外说明。
"""
_STRUCTURED_OUTPUT_ATTEMPTS = 3


def planner_system_prompt() -> str:
    """Return the immutable built-in planner instruction set."""
    return _PLAN_SYSTEM_PROMPT


def image_planner_system_prompt() -> str:
    """图片模式的内置 planner 指令集，与视频指令集互不影响。"""
    return _IMAGE_PLAN_SYSTEM_PROMPT


def _contains_cjk(value: str) -> bool:
    return bool(_CJK.search(value))


def _text_requests_audio(source_text: str) -> bool:
    if _GLOBAL_SILENCE.search(source_text):
        return False
    actionable_text = _NEGATED_AUDIO_INTENT.sub("", source_text)
    actionable_text = _AUDIO_UI_LITERAL.sub("", actionable_text)
    return bool(
        _AUDIO_INTENT.search(actionable_text)
        or _SPOKEN_DIALOGUE.search(actionable_text)
    )


def _task_requests_audio(
    document: NormalizedDocument,
    task: dict[str, Any],
    source_block_ids: list[object],
) -> bool:
    valid_source_ids = {
        block_id for block_id in source_block_ids if isinstance(block_id, str)
    }
    scoped_text = [
        block.text
        for block in document.blocks
        if block.block_id in valid_source_ids and block.text
    ]
    for field_name in ("user_intent", "prompt"):
        value = task.get(field_name)
        if isinstance(value, str) and value:
            scoped_text.append(value)
    return _text_requests_audio("\n".join(scoped_text))


def _normalize_audit_report(report: AuditReport) -> AuditReport:
    normalized_issues: list[str] = []
    for issue in report.issues:
        normalized = issue.strip()
        if normalized.startswith("技术阻断"):
            if not _ACTIONABLE_HUMAN_HANDLING.search(normalized):
                normalized = (
                    f"{normalized.rstrip('。；; ')}；"
                    "人工处理：请补充可执行素材或确认替代方案后再继续。"
                )
            normalized_issues.append(normalized)
            continue
        if any(
            term in normalized
            for term in _GOAL_REJECTING_AUDIT_LANGUAGE
        ):
            for original, replacement in _GOAL_REJECTING_REWRITES.items():
                normalized = normalized.replace(original, replacement)
            if not normalized.startswith(("实施策略：", "风险缓释：")):
                normalized = f"实施策略：{normalized}"
        normalized_issues.append(normalized)
    return report.model_copy(update={"issues": normalized_issues})


def language_validation_message(issues: list[str]) -> str | None:
    fields = [
        issue.partition(":")[0]
        for issue in issues
        if _CJK_ISSUE_SUFFIX in issue
    ]
    if not fields:
        return None
    return f"以下字段{_CJK_ISSUE_SUFFIX}：{'、'.join(dict.fromkeys(fields))}"


def _compact_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _storyboard_requirements(
    document: NormalizedDocument,
) -> dict[str, list[str]]:
    """Return content block IDs for explicitly marked storyboard tables.

    A table is treated as a storyboard when at least two rows begin with an
    explicit ``镜头 N`` marker, or when a ``镜头``/``镜号`` header is followed
    by at least two rows numbered consecutively from 1 in the primary column.
    Only detected shot rows contribute required block IDs; headers are excluded.
    """
    requirements: dict[str, list[str]] = {}
    tables = sorted(
        (
            block
            for block in document.blocks
            if block.block_type == "table"
        ),
        key=lambda block: (block.order, block.block_id),
    )
    for table in tables:
        cells = {
            block.block_id: block
            for block in document.blocks
            if block.block_type == "table_cell"
            and block.parent_id == table.block_id
            and isinstance(block.table_row, int)
        }
        row_content: dict[int, list[Any]] = {}
        cell_content: dict[str, list[Any]] = {}
        for block in document.blocks:
            if block.block_type in {"table", "table_cell"}:
                continue
            cell_id = next(
                (
                    path_part
                    for path_part in block.path
                    if path_part in cells
                ),
                None,
            )
            if cell_id is not None:
                row = cells[cell_id].table_row
                if row is not None:
                    row_content.setdefault(row, []).append(block)
                    cell_content.setdefault(cell_id, []).append(block)

        shot_rows = {
            row
            for row, blocks in row_content.items()
            if any(
                block.block_type == "text"
                and bool(_STORYBOARD_ROW_MARKER.match(block.text))
                for block in blocks
            )
        }
        if len(shot_rows) < 2:
            shot_rows = set()
            header_rows = sorted(
                row
                for row, blocks in row_content.items()
                if any(
                    block.block_type == "text"
                    and bool(_STORYBOARD_HEADER.fullmatch(block.text))
                    for block in blocks
                )
            )
            for header_row in header_rows:
                numbered_rows: list[tuple[int, int]] = []
                for row in sorted(row_content):
                    if row <= header_row:
                        continue
                    row_cells = sorted(
                        (
                            cell
                            for cell in cells.values()
                            if cell.table_row == row
                            and cell.block_id in cell_content
                        ),
                        key=lambda cell: (
                            cell.table_column
                            if cell.table_column is not None
                            else 10**9,
                            cell.order,
                            cell.block_id,
                        ),
                    )
                    if not row_cells:
                        continue
                    primary_blocks = cell_content[row_cells[0].block_id]
                    number_match = next(
                        (
                            _STORYBOARD_ROW_NUMBER.fullmatch(block.text)
                            for block in primary_blocks
                            if block.block_type == "text"
                            and _STORYBOARD_ROW_NUMBER.fullmatch(block.text)
                        ),
                        None,
                    )
                    if number_match is not None:
                        numbered_rows.append(
                            (row, int(number_match.group(1)))
                        )
                numbers = [number for _, number in numbered_rows]
                if (
                    len(numbered_rows) >= 2
                    and numbers == list(range(1, len(numbered_rows) + 1))
                ):
                    shot_rows = {row for row, _ in numbered_rows}
                    break

        if len(shot_rows) < 2:
            continue

        content_blocks = sorted(
            (
                block
                for row, blocks in row_content.items()
                if row in shot_rows
                for block in blocks
            ),
            key=lambda block: (block.order, block.block_id),
        )
        requirements[table.block_id] = [
            block.block_id for block in content_blocks
        ]
    return requirements


def _normalize_generated_plan_payload(
    payload: dict[str, Any],
    document: NormalizedDocument,
) -> list[str]:
    issues: list[str] = []
    tasks = payload.get("tasks")
    if not isinstance(tasks, list):
        return issues
    valid_block_ids = {block.block_id for block in document.blocks}
    mime_types = {
        asset.asset_id: asset.mime_type
        for asset in document.media_assets
    }
    document_asset_order = {
        asset.asset_id: index
        for index, asset in enumerate(document.media_assets)
    }
    for task_index, task in enumerate(tasks):
        if not isinstance(task, dict):
            continue
        source_block_ids = task.get("source_block_ids")
        if isinstance(source_block_ids, list):
            task["source_block_ids"] = [
                block_id
                for block_id in source_block_ids
                if isinstance(block_id, str)
                and block_id in valid_block_ids
            ]
        if task.get("task_type") == "image_to_video":
            task["image_size"] = None
        raw_references = task.get("reference_images")
        prompt = task.get("prompt")
        if not isinstance(raw_references, list) or not isinstance(prompt, str):
            continue
        try:
            references = canonicalize_references(
                [
                    ImageReference.model_validate(reference)
                    for reference in raw_references
                ]
            )
            selected_asset_ids = [
                reference.asset_id for reference in references
            ]
            if all(
                asset_id in document_asset_order
                for asset_id in selected_asset_ids
            ):
                expected_asset_ids = sorted(
                    selected_asset_ids,
                    key=document_asset_order.__getitem__,
                )
                if selected_asset_ids != expected_asset_ids:
                    issues.append(
                        f"tasks[{task_index}].reference_images: "
                        "必须按文档素材顺序排列，期望 "
                        f"{expected_asset_ids!r}"
                    )
                    continue
            task["prompt"] = remap_asset_id_tokens(
                prompt,
                references,
                mime_types,
            )
            task["reference_images"] = [
                reference.model_dump(mode="json")
                for reference in references
            ]
        except (TypeError, ValueError):
            continue
    return issues


def validate_plan(
    plan: TaskPlan | dict[str, Any],
    document: NormalizedDocument,
    max_output_count: int,
    *,
    enforce_seedance_prompt_contract: bool = False,
    enforce_image_prompt_contract: bool = False,
) -> list[str]:
    payload: Any
    if isinstance(plan, TaskPlan):
        payload = plan.model_dump(mode="json")
    else:
        payload = plan

    issues: list[str] = []
    if not isinstance(payload, dict):
        return ["plan: must be a JSON object"]
    document_summary = payload.get("document_summary")
    if not isinstance(document_summary, str) or not _contains_cjk(
        document_summary
    ):
        issues.append(f"plan.document_summary: {_CJK_ISSUE_SUFFIX}")
    tasks = payload.get("tasks")
    if not isinstance(tasks, list):
        return ["plan.tasks: must be a list"]
    if not tasks:
        return ["plan.tasks: at least one generation task is required"]

    block_ids = {block.block_id for block in document.blocks}
    assets = {asset.asset_id: asset for asset in document.media_assets}
    storyboard_requirements = _storyboard_requirements(document)
    task_ids: set[str] = set()
    referenced_asset_ids: set[str] = set()
    total_output_count = 0
    task_sources: list[
        tuple[str, str | None, set[str], dict[str, Any]]
    ] = []

    for index, task in enumerate(tasks):
        prefix = f"tasks[{index}]"
        if not isinstance(task, dict):
            issues.append(f"{prefix}: must be a JSON object")
            continue

        task_id = task.get("task_id")
        display_id = task_id if isinstance(task_id, str) and task_id else prefix
        if not isinstance(task_id, str) or not task_id:
            issues.append(f"{prefix}.task_id: must be a non-empty string")
        elif task_id in task_ids:
            issues.append(f"{prefix}.task_id: duplicate task_id {task_id}")
        else:
            task_ids.add(task_id)

        for field_name in ("user_intent", "prompt"):
            value = task.get(field_name)
            if not isinstance(value, str) or not _contains_cjk(value):
                issues.append(f"{prefix}.{field_name}: {_CJK_ISSUE_SUFFIX}")

        task_type = task.get("task_type")
        if (
            not isinstance(task_type, str)
            or task_type not in _ALLOWED_TASK_TYPES
        ):
            issues.append(
                f"{prefix}.task_type: must be image_to_image or image_to_video"
            )

        source_block_ids = task.get("source_block_ids")
        if not isinstance(source_block_ids, list) or not source_block_ids:
            issues.append(
                f"{prefix}.source_block_ids: must contain at least one block_id"
            )
            source_block_ids = []
        else:
            for block_id in source_block_ids:
                if not isinstance(block_id, str) or block_id not in block_ids:
                    issues.append(
                        f"{prefix}.source_block_ids: unknown block_id {block_id!r}"
                    )
        valid_source_ids = {
            block_id
            for block_id in source_block_ids
            if isinstance(block_id, str) and block_id in block_ids
        }
        task_sources.append(
            (
                display_id,
                task_type if isinstance(task_type, str) else None,
                valid_source_ids,
                task,
            )
        )

        references = task.get("reference_images")
        if not isinstance(references, list) or not references:
            issues.append(
                f"{prefix}.reference_images: at least one image is required"
            )
            references = []
        task_reference_ids: list[str] = []
        for reference_index, reference in enumerate(references):
            reference_prefix = (
                f"{prefix}.reference_images[{reference_index}]"
            )
            if not isinstance(reference, dict):
                issues.append(f"{reference_prefix}: must be a JSON object")
                continue
            asset_id = reference.get("asset_id")
            if not isinstance(asset_id, str) or not asset_id:
                issues.append(
                    f"{reference_prefix}.asset_id: must be a non-empty string"
                )
                continue
            task_reference_ids.append(asset_id)
            referenced_asset_ids.add(asset_id)
            asset = assets.get(asset_id)
            if asset is None:
                issues.append(
                    f"{reference_prefix}.asset_id: unknown asset_id {asset_id}"
                )
                continue
            if (
                asset.download_error is not None
                or asset.size <= 0
                or not asset.sha256
                or not asset.local_path.is_file()
            ):
                issues.append(
                    f"{reference_prefix}.asset_id: asset {asset_id} download failed"
                )
            role = reference.get("role")
            mime_matches_role = (
                asset.mime_type.startswith("image/")
                and role in {"reference_image", "first_frame", "last_frame"}
            ) or (
                asset.mime_type.startswith("video/")
                and role == "reference_video"
            ) or (
                asset.mime_type.startswith("audio/")
                and role == "reference_audio"
            )
            if not mime_matches_role:
                expected_mime = (
                    "image"
                    if role in {"reference_image", "first_frame", "last_frame"}
                    else "video"
                    if role == "reference_video"
                    else "audio"
                )
                issues.append(
                    f"{reference_prefix}.asset_id: asset {asset_id} must have "
                    f"{expected_mime} MIME for role {role}"
                )
        duplicate_reference_ids = {
            asset_id
            for asset_id in task_reference_ids
            if task_reference_ids.count(asset_id) > 1
        }
        for asset_id in sorted(duplicate_reference_ids):
            issues.append(
                f"{prefix}.reference_images: duplicate asset_id {asset_id}"
            )

        reference_mode = task.get("reference_mode")
        if reference_mode not in {None, "multi_reference", "first_last_frame"}:
            issues.append(
                f"{prefix}.reference_mode: must be multi_reference or first_last_frame"
            )
        roles = [
            reference.get("role")
            for reference in references
            if isinstance(reference, dict)
        ]
        if task_type == "image_to_image":
            if reference_mode == "first_last_frame":
                issues.append(
                    f"{prefix}.reference_mode: 图生图只能使用多参考模式"
                )
            elif any(role != "reference_image" for role in roles):
                issues.append(
                    f"{prefix}.reference_images: 图生图只接受普通参考图"
                )
        elif task_type == "image_to_video":
            if reference_mode == "first_last_frame":
                ordered_roles = [
                    reference.get("role")
                    for reference in sorted(
                        (
                            reference
                            for reference in references
                            if isinstance(reference, dict)
                            and isinstance(reference.get("order"), int)
                        ),
                        key=lambda reference: reference["order"],
                    )
                ]
                if ordered_roles != ["first_frame", "last_frame"]:
                    issues.append(
                        f"{prefix}.reference_mode: 首尾帧模式必须且只能按顺序指定一张首帧和一张尾帧"
                    )
            elif reference_mode == "multi_reference" and any(
                role not in {
                    "reference_image",
                    "reference_video",
                    "reference_audio",
                }
                for role in roles
            ):
                issues.append(
                    f"{prefix}.reference_mode: 多参考模式只能使用普通参考图、参考视频或参考音频"
                )

        if task_type == "image_to_image":
            if not isinstance(task.get("image_size"), str) or not task.get(
                "image_size"
            ):
                issues.append(
                    f"{prefix}.image_size: required for image_to_image"
                )
            for field_name in ("duration", "resolution", "generate_audio"):
                if task.get(field_name) is not None:
                    issues.append(
                        f"{prefix}.{field_name}: not allowed for image_to_image"
                    )
        elif task_type == "image_to_video":
            if not isinstance(task.get("duration"), int) or isinstance(
                task.get("duration"), bool
            ):
                issues.append(
                    f"{prefix}.duration: required for image_to_video"
                )
            if not isinstance(task.get("resolution"), str) or not task.get(
                "resolution"
            ):
                issues.append(
                    f"{prefix}.resolution: required for image_to_video"
                )
            if task.get("image_size") is not None:
                issues.append(
                    f"{prefix}.image_size: not allowed for image_to_video"
                )
            generate_audio = task.get("generate_audio")
            if generate_audio is not None and not isinstance(
                generate_audio, bool
            ):
                issues.append(
                    f"{prefix}.generate_audio: must be true, false, or omitted"
                )
            elif (
                _task_requests_audio(document, task, source_block_ids)
                and generate_audio is not True
            ):
                issues.append(
                    f"{prefix}.generate_audio: "
                    "文档明确要求对白、音效、配音、环境音或音乐时必须为 true"
                )

        output_count = task.get("output_count", 1)
        if (
            not isinstance(output_count, int)
            or isinstance(output_count, bool)
            or output_count < 1
        ):
            issues.append(f"{prefix}.output_count: must be an integer >= 1")
        else:
            total_output_count += output_count

    if total_output_count > max_output_count:
        issues.append(
            "plan.total output_count: "
            f"{total_output_count} exceeds max_output_count {max_output_count}"
        )

    raw_exclusions = payload.get("excluded_assets", [])
    excluded_asset_ids: list[str] = []
    if not isinstance(raw_exclusions, list):
        issues.append("plan.excluded_assets: must be a list")
        raw_exclusions = []
    for index, exclusion in enumerate(raw_exclusions):
        prefix = f"plan.excluded_assets[{index}]"
        if not isinstance(exclusion, dict):
            issues.append(f"{prefix}: must be a JSON object")
            continue
        asset_id = exclusion.get("asset_id")
        reason = exclusion.get("reason")
        if not isinstance(asset_id, str) or not asset_id:
            issues.append(f"{prefix}.asset_id: must be a non-empty string")
            continue
        excluded_asset_ids.append(asset_id)
        asset = assets.get(asset_id)
        if asset is None:
            issues.append(f"{prefix}.asset_id: unknown asset_id {asset_id}")
        elif asset.download_error is not None:
            issues.append(
                f"{prefix}.asset_id: failed asset {asset_id} cannot be excluded"
            )
        if not isinstance(reason, str) or not _contains_cjk(reason):
            issues.append(f"{prefix}.reason: {_CJK_ISSUE_SUFFIX}")

    duplicate_exclusions = {
        asset_id
        for asset_id in excluded_asset_ids
        if excluded_asset_ids.count(asset_id) > 1
    }
    for asset_id in sorted(duplicate_exclusions):
        issues.append(
            f"plan.excluded_assets: duplicate asset_id {asset_id}"
        )

    excluded_set = set(excluded_asset_ids)
    for asset_id in sorted(referenced_asset_ids.intersection(excluded_set)):
        issues.append(
            "plan.excluded_assets: referenced asset "
            f"{asset_id} cannot also be excluded"
        )
    successful_asset_ids = {
        asset.asset_id
        for asset in document.media_assets
        if asset.download_error is None
    }
    uncovered = (
        successful_asset_ids - referenced_asset_ids - excluded_set
    )
    for asset_id in sorted(uncovered):
        issues.append(
            f"plan.asset_coverage: uncovered successful asset {asset_id}"
        )

    storyboard_task_names: set[str] = set()
    for table_id, required_ids in storyboard_requirements.items():
        relevant_ids = {table_id, *required_ids}
        relevant_tasks = [
            task
            for task in task_sources
            if task[2].intersection(relevant_ids)
        ]
        storyboard_task_names.update(task[0] for task in relevant_tasks)
        if len(relevant_tasks) != 1:
            issues.append(
                f"storyboard table {table_id}: exactly one image_to_video task "
                f"must cover all content blocks {required_ids!r}; "
                f"found {len(relevant_tasks)}"
            )
            continue

        task_name, task_type, source_ids, _ = relevant_tasks[0]
        if task_type != "image_to_video":
            issues.append(
                f"storyboard table {table_id}: task {task_name} must be "
                "image_to_video"
            )
        missing_ids = [
            block_id
            for block_id in required_ids
            if block_id not in source_ids
        ]
        if missing_ids:
            issues.append(
                f"storyboard table {table_id}: task {task_name} missing "
                f"source_block_ids {missing_ids!r}"
            )

    if enforce_seedance_prompt_contract:
        mime_types = {
            asset_id: asset.mime_type for asset_id, asset in assets.items()
        }
        for task_name, task_type, _, raw_task in task_sources:
            if task_type != "image_to_video":
                continue
            prompt = raw_task.get("prompt")
            user_intent = raw_task.get("user_intent")
            narrative_multishot = (
                isinstance(prompt, str)
                and has_multiple_shot_markers(prompt)
            ) or (
                isinstance(user_intent, str)
                and any(
                    keyword in user_intent
                    for keyword in ("分镜", "多镜头", "多个镜头")
                )
            )
            issues.extend(
                validate_seedance_prompt(
                    raw_task,
                    mime_types,
                    require_storyboard=(
                        task_name in storyboard_task_names
                        or narrative_multishot
                    ),
                )
            )

    if enforce_image_prompt_contract:
        mime_types = {
            asset_id: asset.mime_type for asset_id, asset in assets.items()
        }
        for _, task_type, _, raw_task in task_sources:
            if task_type != "image_to_image":
                continue
            issues.extend(validate_image_prompt(raw_task, mime_types))

    return issues


class DeepSeekPlanner:
    def __init__(self, model: Any, *, max_output_count: int = 4) -> None:
        self._plan_model = model.bind(
            response_format={"type": "json_object"},
            extra_body={
                "thinking": {"type": "enabled"},
                "reasoning_effort": "high",
            },
        )
        self._audit_model = model.bind(
            response_format={"type": "json_object"},
            extra_body={
                "thinking": {"type": "disabled"},
            },
        )
        self.max_output_count = max_output_count

    async def plan(
        self,
        document: NormalizedDocument,
        visions: list[VisionDescription],
        feedback: str | None = None,
        system_prompt: str | None = None,
        exact_system_prompt: str | None = None,
        mode: PlanningMode = "video",
        character_context: str | None = None,
    ) -> TaskPlan:
        image_mode = mode == "image"
        if exact_system_prompt is not None:
            effective_system_prompt = exact_system_prompt
        elif image_mode:
            effective_system_prompt = (
                image_planner_system_prompt()
                if system_prompt is None
                else f"{_PORTAL_IMAGE_PLANNER_CONTRACT}{system_prompt}"
            )
        else:
            effective_system_prompt = (
                planner_system_prompt()
                if system_prompt is None
                else f"{_PORTAL_PLANNER_CONTRACT}{system_prompt}"
            )
        user_content = self._planning_prompt(document, visions, feedback, mode)
        if character_context:
            user_content = (
                f"{user_content}\n\n"
                "【素材库已匹配角色】\n"
                f"{character_context}\n"
                "画面出现上述角色时，必须把对应 asset_id 挂进 reference_images"
                "（role=reference_image），并在 prompt 中沿用该角色的既有形象，"
                "不要用文档里的普通图片替代。"
            )
        messages = [
            {"role": "system", "content": effective_system_prompt},
            {"role": "user", "content": user_content},
        ]

        def validate_payload(payload: dict[str, Any]) -> list[str]:
            normalization_issues = _normalize_generated_plan_payload(
                payload,
                document,
            )
            return [
                *normalization_issues,
                *validate_plan(
                    payload,
                    document,
                    self.max_output_count,
                    enforce_seedance_prompt_contract=not image_mode,
                    enforce_image_prompt_contract=image_mode,
                ),
            ]

        result = await self._invoke_with_repair(
            model=self._plan_model,
            messages=messages,
            schema=TaskPlan,
            deterministic_validator=validate_payload,
            document_id=document.document_id,
            operation="plan",
        )
        return result

    async def audit(
        self,
        document: NormalizedDocument,
        plan: TaskPlan,
    ) -> AuditReport:
        messages = [
            {"role": "system", "content": _AUDIT_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": self._audit_prompt(document, plan),
            },
        ]
        report = await self._invoke_with_repair(
            model=self._audit_model,
            messages=messages,
            schema=AuditReport,
            deterministic_validator=lambda payload: [],
            document_id=document.document_id,
            operation="audit",
        )
        return _normalize_audit_report(report)

    def _planning_prompt(
        self,
        document: NormalizedDocument,
        visions: list[VisionDescription],
        feedback: str | None,
        mode: PlanningMode = "video",
    ) -> str:
        table_ids = {
            block.block_id
            for block in document.blocks
            if block.block_type == "table"
        }
        table_blocks = [
            block.model_dump(mode="json")
            for block in document.blocks
            if block.block_id in table_ids
            or any(part in table_ids for part in block.path)
        ]
        media_references = [
            {
                "asset_id": asset.asset_id,
                "source_block_id": asset.source_block_id,
                "mime_type": asset.mime_type,
                "width": asset.width,
                "height": asset.height,
                "download_succeeded": (
                    asset.download_error is None
                    and asset.size > 0
                    and bool(asset.sha256)
                    and asset.local_path.is_file()
                ),
            }
            for asset in document.media_assets
        ]
        vision_payload = [
            vision.model_dump(mode="json") for vision in visions
        ]
        schema = TaskPlan.model_json_schema()
        if mode == "image":
            # 图片模式不能带视频指令：用户提示词离生成更近，Seedance 的
            # prompt 格式要求会压过 system prompt 里的图片模板，导致模板
            # 骨架整段失效（实测产出的 prompt 一句模板都没有）。
            return "\n".join(
                [
                    "请把以下文档规划为可执行的图片生成任务。",
                    "允许的 task_type 只有 image_to_image。",
                    (
                        "图片匹配优先级：文档显式引用或同一表格行 > "
                        "同一章节/路径 > 视觉描述语义匹配 > 文档顺序；"
                        "不得虚构图片。"
                    ),
                    (
                        "逐张读取全部视觉描述，先理解素材的主体、场景、风格、"
                        "构图和可能用途，再决定它属于角色参考、场景参考、"
                        "概念示意还是风格参考；不得机械平均分配。"
                    ),
                    (
                        "prompt 必须严格套用 system 指令里的模板骨架，"
                        "固定句式原样保留、顺序不变，只替换尖括号槽位。"
                    ),
                    (
                        "每个下载成功的素材必须且只能归入任务 "
                        "reference_images 或 excluded_assets；未使用素材必须"
                        "写入 excluded_assets，reason 必须用中文说明。"
                        "下载失败素材不得引用或排除。"
                    ),
                    f"max_output_count={self.max_output_count}",
                    f"document_id={document.document_id}",
                    "稳定 text_view（含 [block:*] / [image:*] 引用）：",
                    document.text_view,
                    f"序列化表格及后代 blocks={_compact_json(table_blocks)}",
                    f"可用素材引用={_compact_json(media_references)}",
                    f"全部视觉描述={_compact_json(vision_payload)}",
                    f"用户反馈={_compact_json(feedback)}",
                    f"TaskPlan JSON Schema={_compact_json(schema)}",
                    "只返回符合 Schema 的 JSON 对象。",
                ]
            )
        return "\n".join(
            [
                "请把以下文档规划为可执行生成任务。",
                "允许的 task_type 只有 image_to_image 和 image_to_video。",
                (
                    "图片匹配优先级：文档显式引用或同一表格行 > "
                    "同一章节/路径 > 视觉描述语义匹配 > 文档顺序；不得虚构图片。"
                ),
                (
                    "逐张读取全部视觉描述，先理解素材的主体、场景、风格、"
                    "构图、动作和可能用途，再按“同一分镜行/Block > "
                    "同一章节路径 > 主体与动作语义匹配 > 场景和风格匹配”"
                    "分配到镜头；不得机械平均分配。"
                ),
                (
                    "Seedance prompt 使用 @图片N/@视频N/@音频N，"
                    "为每个素材写具体中文语义；多分镜使用镜头 1/2/3，"
                    "每个镜头直接绑定相关素材，禁止绝对秒数，并补齐画质、"
                    "稳定、无水印和无 Logo 约束。"
                ),
                (
                    "分镜合并规则：同一分镜表的多行必须合并为一个视频任务，"
                    "在一个 prompt 中按镜头顺序描述；不得按镜头拆成多个付费任务。"
                ),
                (
                    "自由叙述按完整意图生成任务；混合图片/视频需求按不同意图"
                    "分别生成对应任务，不要错误合并。"
                ),
                (
                    "每个下载成功的素材必须且只能归入任务 reference_images "
                    "或 excluded_assets；未使用素材必须写入 excluded_assets，"
                    "reason 必须用中文说明。下载失败素材不得引用或排除。"
                ),
                (
                    "文档明确要求对白、台词、音效、配音、环境音、BGM 或音乐时，"
                    "对应 image_to_video 任务的 generate_audio 必须为 true；"
                    "明确要求静音或无音频时才可设为 false。"
                ),
                f"max_output_count={self.max_output_count}",
                f"document_id={document.document_id}",
                "稳定 text_view（含 [block:*] / [image:*] 引用）：",
                document.text_view,
                f"序列化表格及后代 blocks={_compact_json(table_blocks)}",
                f"可用素材引用={_compact_json(media_references)}",
                f"全部视觉描述={_compact_json(vision_payload)}",
                f"用户反馈={_compact_json(feedback)}",
                f"TaskPlan JSON Schema={_compact_json(schema)}",
                "只返回符合 Schema 的 JSON 对象。",
            ]
        )

    @staticmethod
    def _audit_prompt(
        document: NormalizedDocument,
        plan: TaskPlan,
    ) -> str:
        return "\n".join(
            [
                "独立审查以下计划，只报告遗漏、冲突、虚构和供应商限制。",
                "不得改写计划，不得返回修正后的 tasks。",
                (
                    "不要否定或质疑需求目标；对供应商限制给出中文实施策略或"
                    "风险缓释，不使用“无法保证”“不合理”“做不到”等措辞。"
                ),
                f"document_id={document.document_id}",
                f"text_view={document.text_view}",
                f"plan={_compact_json(plan.model_dump(mode='json'))}",
                (
                    "AuditReport JSON Schema="
                    f"{_compact_json(AuditReport.model_json_schema())}"
                ),
                "只返回符合 Schema 的 JSON 对象。",
            ]
        )

    async def _invoke_with_repair(
        self,
        *,
        model: Any,
        messages: list[dict[str, Any]],
        schema: type[BaseModel],
        deterministic_validator: Callable[[dict[str, Any]], list[str]],
        document_id: str,
        operation: str,
    ) -> Any:
        repair_message: dict[str, str] | None = None
        last_errors: list[str] = []
        for attempt in range(_STRUCTURED_OUTPUT_ATTEMPTS):
            request_messages = list(messages)
            if repair_message is not None:
                request_messages.append(repair_message)

            model_error: AgentError | None = None
            response: object | None = None
            try:
                with tracing_context(enabled=False, parent=False):
                    response = await model.ainvoke(
                        request_messages,
                        config={"callbacks": []},
                    )
            except Exception as exc:
                model_error = self._model_error(document_id, operation, exc)
            if model_error is not None:
                raise model_error

            raw_content = self._response_content(response)
            parsed, last_errors = self._parse_and_validate(
                raw_content,
                schema,
                deterministic_validator,
            )
            if parsed is not None:
                return parsed

            if attempt + 1 < _STRUCTURED_OUTPUT_ATTEMPTS:
                repair_message = {
                    "role": "user",
                    "content": self._repair_prompt(raw_content, last_errors),
                }

        raise self._validation_error(document_id, operation, last_errors)

    @staticmethod
    def _response_content(response: object | None) -> object:
        if response is None:
            return None
        if isinstance(response, (str, dict)):
            return response
        return getattr(response, "content", None)

    @staticmethod
    def _parse_and_validate(
        raw_content: object,
        schema: type[BaseModel],
        deterministic_validator: Callable[[dict[str, Any]], list[str]],
    ) -> tuple[BaseModel | None, list[str]]:
        if isinstance(raw_content, dict):
            payload: object = raw_content
        elif isinstance(raw_content, str):
            try:
                payload = json.loads(raw_content)
            except (json.JSONDecodeError, TypeError):
                return None, ["response: invalid JSON object"]
        else:
            return None, ["response: missing JSON object"]

        if not isinstance(payload, dict):
            return None, ["response: top-level JSON must be an object"]

        errors = deterministic_validator(payload)
        parsed: BaseModel | None = None
        try:
            parsed = schema.model_validate(payload)
        except ValidationError as exc:
            errors.extend(DeepSeekPlanner._compact_validation_errors(exc))
        if errors:
            return None, errors
        return parsed, []

    @staticmethod
    def _compact_validation_errors(exc: ValidationError) -> list[str]:
        errors = []
        for error in exc.errors(include_url=False, include_input=False):
            location = ".".join(str(part) for part in error["loc"])
            message = str(error["msg"])
            errors.append(f"schema.{location}: {message}"[:240])
        return errors[:12]

    @staticmethod
    def _repair_prompt(raw_content: object, errors: list[str]) -> str:
        if isinstance(raw_content, str):
            raw_text = raw_content
        elif isinstance(raw_content, dict):
            raw_text = _compact_json(raw_content)
        else:
            raw_text = "null"
        concise_errors = "\n".join(f"- {error}" for error in errors[:12])
        instructions = [
            "仅返回修复后的 JSON 对象，不要解释或输出推理过程。",
        ]
        if language_validation_message(errors) is not None:
            instructions.insert(
                0,
                (
                    "本次仅修复语言或报告字段；保持任务数量、任务类型、"
                    "素材引用、参数和原始需求不变，不得新增或发明需求。"
                    "文档明确要求的英文对白、品牌名和 UI 字面量必须原样保留。"
                ),
            )
        return "\n".join(
            [
                "原始输出：",
                raw_text,
                "校验错误：",
                concise_errors,
                *instructions,
            ]
        )

    @classmethod
    def _model_error(
        cls,
        document_id: str,
        operation: str,
        exc: Exception,
    ) -> AgentError:
        status_code = cls._status_code(exc)
        exception_name = type(exc).__name__
        lowered_name = exception_name.lower()
        technical_detail = (
            f"document_id={document_id}; operation={operation}; "
            f"cause={exception_name}"
        )
        if status_code is not None:
            technical_detail += f"; status={status_code}"
        retryable = (
            status_code == 429
            or (status_code is not None and status_code >= 500)
            or isinstance(
                exc,
                (httpx.TransportError, TimeoutError, ConnectionError),
            )
            or lowered_name in {
                "apiconnectionerror",
                "apitimeouterror",
                "ratelimiterror",
            }
        )
        if retryable:
            return AgentError(
                ErrorDetail(
                    category=ErrorCategory.TRANSIENT,
                    message=(
                        "需求规划模型暂时不可用"
                        f"（document_id={document_id}）"
                    ),
                    technical_detail=technical_detail,
                    retryable=True,
                )
            )
        return AgentError(
            ErrorDetail(
                category=ErrorCategory.PROVIDER_TERMINAL,
                message=f"需求规划模型调用失败（document_id={document_id}）",
                technical_detail=technical_detail,
                retryable=False,
            )
        )

    @staticmethod
    def _validation_error(
        document_id: str,
        operation: str,
        errors: list[str],
    ) -> AgentError:
        language_failure = language_validation_message(errors)
        message_prefix = (
            "模型三次返回的 JSON 均未通过中文规划校验："
            f"{language_failure}"
            if language_failure
            else "模型三次返回的 JSON 均未通过校验"
        )
        return AgentError(
            ErrorDetail(
                category=ErrorCategory.VALIDATION,
                message=f"{message_prefix}（document_id={document_id}）",
                technical_detail=(
                    f"document_id={document_id}; operation={operation}; "
                    f"attempts={_STRUCTURED_OUTPUT_ATTEMPTS}; "
                    f"error_count={len(errors)}; "
                    # 只记 error_count 时无法判断是哪条契约卡住，排查只能靠猜。
                    f"issues={' | '.join(errors[:8])}"
                ),
                retryable=False,
            )
        )

    @staticmethod
    def _status_code(exc: Exception) -> int | None:
        status_code = getattr(exc, "status_code", None)
        if isinstance(status_code, int):
            return status_code
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
        return status_code if isinstance(status_code, int) else None
