"""M7.4e：完整 turn 边界上的基础安全切点测试。"""

from __future__ import annotations

import pytest

from mini_pi.context.compaction import CutPoint, find_cut_point
from mini_pi.context.tokens import estimate_tokens
from mini_pi.llm.types import (
    AssistantMessage,
    Message,
    ToolCall,
    ToolMessage,
    UserMessage,
)


def tokens_of(messages: list[Message]) -> int:
    return estimate_tokens(messages).tokens


def test_empty_history_has_no_cut_point() -> None:
    """空历史没有可压缩内容。"""
    assert find_cut_point([], keep_recent_tokens=10) is None


def test_everything_within_budget_has_no_cut_point() -> None:
    """全部消息都在预算内时不产生切点。"""
    messages: list[Message] = [UserMessage(content="aaaa"), AssistantMessage(content="bbbb")]

    assert find_cut_point(messages, keep_recent_tokens=2) is None


def test_trailing_user_message_starts_kept_region_at_user() -> None:
    """尾随 user 时保留区间从最近一轮 user 开始。"""
    messages: list[Message] = [
        UserMessage(content="aaaa"),
        AssistantMessage(content="bbbb"),
        UserMessage(content="cccc"),
    ]

    assert find_cut_point(messages, keep_recent_tokens=1) == CutPoint(
        start_index=2, boundary="user", kept_tokens=1
    )


def test_trailing_assistant_uses_complete_assistant_boundary() -> None:
    """上一轮整体超预算时，允许从完整 assistant turn 边界切。"""
    messages: list[Message] = [UserMessage(content="aaaa"), AssistantMessage(content="bbbb")]

    assert find_cut_point(messages, keep_recent_tokens=1) == CutPoint(
        start_index=1, boundary="assistant", kept_tokens=1
    )


def test_exact_budget_hit_keeps_that_turn() -> None:
    """累计刚好等于预算时该 turn 仍然保留，不提前切。"""
    messages: list[Message] = [
        UserMessage(content="aaaa"),
        AssistantMessage(content="bbbb"),
        UserMessage(content="cccc"),
        AssistantMessage(content="dddd"),
    ]

    assert find_cut_point(messages, keep_recent_tokens=2) == CutPoint(
        start_index=2, boundary="user", kept_tokens=2
    )


def test_prefers_user_boundary_when_turn_fits_budget() -> None:
    """单个 turn 未超预算时，宁可略超预算也要从 user 边界开始。"""
    messages: list[Message] = [
        UserMessage(content="0000"),
        AssistantMessage(content="1111"),
        UserMessage(content="2222"),
        AssistantMessage(content="3333"),
        UserMessage(content="4444"),
        AssistantMessage(content="5555"),
    ]

    cut = find_cut_point(messages, keep_recent_tokens=3)

    assert cut == CutPoint(start_index=2, boundary="user", kept_tokens=4)


def test_tool_batch_is_never_split() -> None:
    """assistant tool_calls 与全部 tool results 必须整体保留。"""
    call = ToolCall(id="c1", name="read", arguments={"path": "a.txt"})
    assistant_calls = AssistantMessage(tool_calls=[call])
    tool_result = ToolMessage(tool_call_id="c1", name="read", content="data")
    final = AssistantMessage(content="dddd")
    messages: list[Message] = [
        UserMessage(content="aaaa"),
        AssistantMessage(content="bbbb"),
        UserMessage(content="cccc"),
        assistant_calls,
        tool_result,
        final,
    ]
    budget = tokens_of([assistant_calls, tool_result, final])

    cut = find_cut_point(messages, keep_recent_tokens=budget)

    assert cut is not None
    assert cut.start_index == 3
    assert cut.boundary == "assistant"
    kept = messages[cut.start_index :]
    assert assistant_calls in kept and tool_result in kept


def test_single_turn_over_budget_is_kept_whole() -> None:
    """整个历史只有一轮且超预算时，找不到安全切点。"""
    messages: list[Message] = [UserMessage(content="x" * 400)]

    assert find_cut_point(messages, keep_recent_tokens=1) is None


def test_overlong_earlier_turn_can_cut_at_assistant() -> None:
    """早先的单 turn 超预算时，保留其 assistant 回复而摘要掉超长 user。"""
    messages: list[Message] = [UserMessage(content="x" * 400), AssistantMessage(content="bbbb")]

    assert find_cut_point(messages, keep_recent_tokens=1) == CutPoint(
        start_index=1, boundary="assistant", kept_tokens=1
    )


def test_keep_recent_tokens_must_be_positive() -> None:
    """非法预算直接报错，不进入切点计算。"""
    with pytest.raises(ValueError, match="keep_recent_tokens"):
        find_cut_point([UserMessage(content="aaaa")], keep_recent_tokens=0)
