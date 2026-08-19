"""语义匹配层：把文档里的人物描述对上素材库条目。

精确匹配（domain/character_matcher.py）只认字面命中的名字与别名。文档里
常见的是「金发那位穿着晚宴礼服的女性」这类描述式指代，或者同一角色的多个
着装变体需要按上下文挑一个——这些交给模型判断。

已由精确匹配确认的结果作为「锚点」一起传给模型，避免它重复推理，也避免
它把已经挂好的角色又挂一遍。

模型输出一律经过白名单过滤：只认真实存在的 asset_id，且置信度达标。模型
幻觉出的 asset_id 会被丢弃，不允许污染计划。
"""

from dataclasses import dataclass
import json
from typing import Any

from pydantic import BaseModel, Field

from feishu_generation_agent.domain.asset_library import CharacterAsset
from feishu_generation_agent.domain.character_matcher import CharacterMatch
from feishu_generation_agent.domain.errors import (
    AgentError,
    ErrorCategory,
    ErrorDetail,
)


_SYSTEM_PROMPT = """你是角色素材匹配器。
判断需求文档里出现的人物分别对应素材库中的哪个条目，只输出 JSON。
只能引用给定素材库列表里的 asset_id，禁止编造。
同一角色有多个着装/状态变体时，按文档描述挑最贴合的那一个。
文档里出现但素材库没有的人物，放进 unresolved_candidates。
已在「已匹配角色」中列出的条目不要重复输出。
不要输出思维过程、Markdown 或 JSON 之外的说明。
"""

_DEFAULT_MIN_CONFIDENCE = 0.5


class _RawMatch(BaseModel):
    asset_id: str = ""
    block_ids: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    reason: str = ""


class _RawCandidate(BaseModel):
    proposed_name: str = ""
    block_ids: list[str] = Field(default_factory=list)
    reason: str = ""


class _RawResponse(BaseModel):
    matches: list[_RawMatch] = Field(default_factory=list)
    unresolved_candidates: list[_RawCandidate] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class SemanticMatch:
    asset_id: str
    block_ids: tuple[str, ...]
    confidence: float
    reason: str


@dataclass(frozen=True, slots=True)
class UnresolvedCandidate:
    proposed_name: str
    block_ids: tuple[str, ...]
    reason: str


@dataclass(frozen=True, slots=True)
class SemanticMatchResult:
    matches: tuple[SemanticMatch, ...] = ()
    unresolved_candidates: tuple[UnresolvedCandidate, ...] = ()


class DeepSeekCharacterMatcher:
    def __init__(
        self,
        model: Any,
        *,
        min_confidence: float = _DEFAULT_MIN_CONFIDENCE,
        max_library_entries: int = 200,
    ) -> None:
        self._model = model.bind(response_format={"type": "json_object"})
        self._min_confidence = min_confidence
        self._max_library_entries = max_library_entries

    async def match(
        self,
        document_text: str,
        library: list[CharacterAsset],
        *,
        anchors: list[CharacterMatch],
    ) -> SemanticMatchResult:
        if not document_text.strip():
            # 没有文档内容，模型无从判断，省一次 API 调用。
            # 注意：素材库为空时不能短路——文档里的未知角色仍要被识别成
            # unresolved_candidates，供后续自动入库。
            return SemanticMatchResult()

        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": self._user_prompt(document_text, library, anchors),
            },
        ]
        try:
            response = await self._model.ainvoke(
                messages, config={"callbacks": []}
            )
        except Exception as exc:
            raise self._error(
                "角色素材语义匹配调用失败",
                f"cause={type(exc).__name__}",
                ErrorCategory.TRANSIENT,
                retryable=True,
            ) from exc

        payload = self._parse(getattr(response, "content", response))
        known = {asset.asset_id for asset in library}
        anchored = {anchor.asset_id for anchor in anchors}

        matches = tuple(
            SemanticMatch(
                asset_id=item.asset_id,
                block_ids=tuple(item.block_ids),
                confidence=item.confidence,
                reason=item.reason,
            )
            for item in payload.matches
            if item.asset_id in known
            and item.asset_id not in anchored
            and item.confidence >= self._min_confidence
        )
        candidates = tuple(
            UnresolvedCandidate(
                proposed_name=item.proposed_name.strip(),
                block_ids=tuple(item.block_ids),
                reason=item.reason,
            )
            for item in payload.unresolved_candidates
            if item.proposed_name.strip()
        )
        return SemanticMatchResult(
            matches=matches, unresolved_candidates=candidates
        )

    def _user_prompt(
        self,
        document_text: str,
        library: list[CharacterAsset],
        anchors: list[CharacterMatch],
    ) -> str:
        sections = [f"【需求文档】\n{document_text}"]
        if anchors:
            anchor_lines = "\n".join(
                f"- {anchor.name} / {anchor.variant}"
                f"（asset_id={anchor.asset_id}）"
                for anchor in anchors
            )
            sections.append(f"【已匹配角色，不要重复输出】\n{anchor_lines}")
        if library:
            entries = library[: self._max_library_entries]
            library_lines = "\n".join(
                f"- asset_id={asset.asset_id}；姓名={asset.name}；"
                f"变体={asset.variant}；别名={'/'.join(asset.aliases) or '无'}；"
                f"描述={asset.description or '无'}；"
                f"标签={'/'.join(asset.tags) or '无'}"
                for asset in entries
            )
            sections.append(f"【素材库】\n{library_lines}")
        sections.append(
            "【输出格式】\n"
            '{"matches": [{"asset_id": "...", "block_ids": ["..."], '
            '"confidence": 0.0, "reason": "..."}], '
            '"unresolved_candidates": [{"proposed_name": "...", '
            '"block_ids": ["..."], "reason": "..."}]}'
        )
        return "\n\n".join(sections)

    def _parse(self, raw: object) -> _RawResponse:
        if isinstance(raw, dict):
            payload: object = raw
        elif isinstance(raw, str):
            try:
                payload = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                raise self._error(
                    "角色素材语义匹配返回的不是合法 JSON",
                    "cause=invalid_json",
                    ErrorCategory.VALIDATION,
                ) from None
        else:
            raise self._error(
                "角色素材语义匹配没有返回内容",
                "cause=empty_response",
                ErrorCategory.VALIDATION,
            )
        try:
            return _RawResponse.model_validate(payload)
        except Exception as exc:
            raise self._error(
                "角色素材语义匹配返回结构不符合约定",
                "cause=schema_mismatch",
                ErrorCategory.VALIDATION,
            ) from exc

    @staticmethod
    def _error(
        message: str,
        technical_detail: str,
        category: ErrorCategory,
        *,
        retryable: bool = False,
    ) -> AgentError:
        return AgentError(
            ErrorDetail(
                category=category,
                message=message,
                technical_detail=technical_detail,
                retryable=retryable,
            )
        )
