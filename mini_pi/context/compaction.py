"""压缩切点：只在完整 turn 边界上选择保留区间起点。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from mini_pi.context.tokens import estimate_tokens
from mini_pi.llm.types import Message, ToolMessage

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


def find_cut_point(
    messages: Sequence[Message], *, keep_recent_tokens: int
) -> CutPoint | None:
    """在完整 turn 边界上找保留区间起点；找不到安全切点时返回 None。

    规则：
    - 从最新往前累计 token，直到加入下一段会超出 `keep_recent_tokens`
    - tool results 始终与对应 assistant 归为同一段，永不拆散
    - 优先 user 边界；仅当该 user turn 自身已超预算时才允许从 assistant 边界切
    - 全部消息都在预算内、或切点落到 `0`（无可摘要内容）时返回 None
    """
    if keep_recent_tokens <= 0:
        raise ValueError("keep_recent_tokens must be > 0")
    items = list(messages)
    if not items:
        return None

    segments = _segment(items)
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

    if items[start_index].role not in _BOUNDARY_ROLES:
        # system 等非边界段不能作为起点，向更晚的候选推进
        advanced = _next_boundary_segment(segments, start_index, items)
        if advanced is None:
            return None
        start_index = advanced.start
        kept_tokens = _tokens_from(segments, start_index)

    if items[start_index].role == "assistant":
        user_segment = _previous_user_segment(segments, start_index, items)
        if user_segment is not None and _turn_tokens(segments, user_segment, items) <= keep_recent_tokens:
            # 该 user turn 未超预算：宁可略超也保持 turn 完整
            start_index = user_segment.start
            kept_tokens = _tokens_from(segments, start_index)

    if start_index == 0:
        return None
    role = items[start_index].role
    if role not in _BOUNDARY_ROLES:
        return None
    return CutPoint(
        start_index=start_index,
        boundary="user" if role == "user" else "assistant",
        kept_tokens=kept_tokens,
    )


def _segment(messages: Sequence[Message]) -> list[_Segment]:
    """把消息切成原子段：tool result 追加到前一段，其余各自起段。"""
    segments: list[_Segment] = []
    for index, message in enumerate(messages):
        tokens = estimate_tokens([message]).tokens
        if isinstance(message, ToolMessage) and segments:
            segments[-1].tokens += tokens
            continue
        segments.append(_Segment(start=index, role=message.role, tokens=tokens))
    return segments


def _tokens_from(segments: Sequence[_Segment], start_index: int) -> int:
    return sum(segment.tokens for segment in segments if segment.start >= start_index)


def _next_boundary_segment(
    segments: Sequence[_Segment], start_index: int, messages: Sequence[Message]
) -> _Segment | None:
    for segment in segments:
        if segment.start >= start_index and messages[segment.start].role in _BOUNDARY_ROLES:
            return segment
    return None


def _previous_user_segment(
    segments: Sequence[_Segment], start_index: int, messages: Sequence[Message]
) -> _Segment | None:
    user_segment: _Segment | None = None
    for segment in segments:
        if segment.start >= start_index:
            break
        if messages[segment.start].role == "user":
            user_segment = segment
    return user_segment


def _turn_tokens(
    segments: Sequence[_Segment], user_segment: _Segment, messages: Sequence[Message]
) -> int:
    """从 user 边界到下一个 user 之前的整轮 token，用于判断单 turn 是否超预算。"""
    total = 0
    for segment in segments:
        if segment.start < user_segment.start:
            continue
        if segment.start > user_segment.start and messages[segment.start].role == "user":
            break
        total += segment.tokens
    return total
