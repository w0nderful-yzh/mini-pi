"""压缩切点：只在完整 user turn 或完整工具轮边界选择保留区间起点。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from mini_pi.context.tokens import estimate_tokens
from mini_pi.llm.types import AssistantMessage, Message, ToolMessage

# 允许作为切点的消息 role；system/tool 不能作为保留区间起点
CutBoundary = Literal["user", "assistant"]
_BOUNDARY_ROLES = frozenset({"user", "assistant"})


@dataclass(frozen=True, slots=True)
class CutPoint:
    """压缩切点：`start_index` 之前进入摘要，之后整体保留。"""

    start_index: int
    boundary: CutBoundary
    kept_tokens: int


@dataclass(slots=True)
class _Segment:
    """不可拆分的原子片段：一条非 tool 消息及其紧随的 tool results。"""

    start: int
    role: str
    tokens: int = field(default=0)
    # 是否可作为保留区间起点（system/tool、结果缺失的工具轮都不行）
    splittable: bool = True
    # assistant 工具调用缺结果：只能整轮保留，不能从该段切开
    has_incomplete_batch: bool = False


def find_cut_point(
    messages: Sequence[Message], *, keep_recent_tokens: int
) -> CutPoint | None:
    """在完整 turn 或完整工具轮边界上找保留区间起点；找不到安全切点时返回 None。

    规则：
    - 消息先切成原子段：assistant 的多工具调用与其全部结果同段，永不拆散
    - 从最新往前累计 token，直到加入下一段会超出 `keep_recent_tokens`
    - 工具结果缺失的工具轮不能作为切点，只能连同所在 turn 整体保留
    - 优先 user 边界；该 user turn 未超预算或含残缺工具轮时才允许从内部切
    - 全部消息都在预算内、或切点落到 `0`（无可摘要内容）时返回 None
    """
    if keep_recent_tokens <= 0:
        raise ValueError("keep_recent_tokens must be > 0")
    items = list(messages)
    if not items:
        return None

    segments = _segment(items)
    by_start = {segment.start: segment for segment in segments}

    kept_tokens = 0
    start_index: int | None = None
    for index in range(len(segments) - 1, -1, -1):
        segment = segments[index]
        if kept_tokens + segment.tokens > keep_recent_tokens and kept_tokens > 0:
            break
        kept_tokens += segment.tokens
        start_index = segment.start
    else:
        # 全部消息都在预算内，无需压缩
        return None
    if start_index is None:
        # 只有 kept_tokens > 0 才会 break，防御性处理不可达分支
        return None

    chosen = by_start[start_index]
    if not chosen.splittable:
        if chosen.has_incomplete_batch:
            # 结果缺失：整轮保留，切点退到所在 user turn
            user_segment = _previous_user_segment(segments, start_index)
            if user_segment is None:
                return None
            start_index = user_segment.start
        else:
            # system 等非边界段不能作起点，向更晚的候选推进
            advanced = _next_splittable_segment(segments, start_index)
            if advanced is None:
                return None
            start_index = advanced.start
        kept_tokens = _tokens_from(segments, start_index)

    if by_start[start_index].role == "assistant":
        user_segment = _previous_user_segment(segments, start_index)
        if user_segment is not None and (
            _turn_has_incomplete_batch(segments, user_segment)
            or _turn_tokens(segments, user_segment) <= keep_recent_tokens
        ):
            # 该 user turn 未超预算（或本身残缺）：宁可略超也保持 turn 完整
            start_index = user_segment.start
            kept_tokens = _tokens_from(segments, start_index)

    if start_index == 0:
        return None
    role = by_start[start_index].role
    if role not in _BOUNDARY_ROLES:
        return None
    return CutPoint(
        start_index=start_index,
        boundary="user" if role == "user" else "assistant",
        kept_tokens=kept_tokens,
    )


def _segment(messages: Sequence[Message]) -> list[_Segment]:
    """切分原子段；assistant 多工具调用消费紧随的 tool results 并校验完整性。"""
    segments: list[_Segment] = []
    index = 0
    while index < len(messages):
        start = index
        message = messages[index]
        tokens = estimate_tokens([message]).tokens
        if isinstance(message, AssistantMessage) and message.tool_calls:
            call_ids = {item.id for item in message.tool_calls}
            result_ids: set[str] = set()
            index += 1
            while index < len(messages) and isinstance(messages[index], ToolMessage):
                tokens += estimate_tokens([messages[index]]).tokens
                result_ids.add(messages[index].tool_call_id)
                index += 1
            complete = result_ids == call_ids
            segments.append(
                _Segment(
                    start=start,
                    role="assistant",
                    tokens=tokens,
                    splittable=complete,
                    has_incomplete_batch=not complete,
                )
            )
            continue
        segments.append(
            _Segment(
                start=start,
                role=message.role,
                tokens=tokens,
                splittable=message.role in _BOUNDARY_ROLES,
            )
        )
        index += 1
    return segments


def _tokens_from(segments: Sequence[_Segment], start_index: int) -> int:
    return sum(segment.tokens for segment in segments if segment.start >= start_index)


def _next_splittable_segment(
    segments: Sequence[_Segment], start_index: int
) -> _Segment | None:
    for segment in segments:
        if segment.start >= start_index and segment.splittable:
            return segment
    return None


def _previous_user_segment(
    segments: Sequence[_Segment], start_index: int
) -> _Segment | None:
    user_segment: _Segment | None = None
    for segment in segments:
        if segment.start >= start_index:
            break
        if segment.role == "user":
            user_segment = segment
    return user_segment


def _turn_segments(
    segments: Sequence[_Segment], user_segment: _Segment
) -> list[_Segment]:
    """从 user 边界到下一个 user 之前的整轮片段。"""
    turn: list[_Segment] = []
    for segment in segments:
        if segment.start < user_segment.start:
            continue
        if segment.start > user_segment.start and segment.role == "user":
            break
        turn.append(segment)
    return turn


def _turn_tokens(segments: Sequence[_Segment], user_segment: _Segment) -> int:
    """整轮 token，用于判断单 turn 是否超预算。"""
    return sum(segment.tokens for segment in _turn_segments(segments, user_segment))


def _turn_has_incomplete_batch(
    segments: Sequence[_Segment], user_segment: _Segment
) -> bool:
    """整轮里是否含有结果缺失的工具轮。"""
    return any(
        segment.has_incomplete_batch
        for segment in _turn_segments(segments, user_segment)
    )
