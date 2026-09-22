"""M7.4f：工具轮 split-turn 切点测试。"""

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


def call(call_id: str) -> ToolCall:
    return ToolCall(id=call_id, name="read", arguments={"path": f"{call_id}.txt"})


def tool(call_id: str, content: str = "data") -> ToolMessage:
    return ToolMessage(tool_call_id=call_id, name="read", content=content)


def tokens_of(messages: list[Message]) -> int:
    return estimate_tokens(messages).tokens


def result_ids_after(messages: list[Message], start_index: int) -> set[str]:
    return {
        message.tool_call_id
        for message in messages[start_index:]
        if isinstance(message, ToolMessage)
    }


def test_multi_tool_batch_kept_as_single_unit() -> None:
    """一次 assistant 的多个 tool calls 与全部结果必须整体保留。"""
    batch = [AssistantMessage(tool_calls=[call("c1"), call("c2")]), tool("c1"), tool("c2")]
    final = AssistantMessage(content="dddd")
    messages: list[Message] = [UserMessage(content="aaaa"), *batch, final]
    budget = tokens_of([*batch, final])

    cut = find_cut_point(messages, keep_recent_tokens=budget)

    assert cut == CutPoint(start_index=1, boundary="assistant", kept_tokens=budget)
    assert result_ids_after(messages, cut.start_index) == {"c1", "c2"}


def test_split_at_second_tool_round_boundary() -> None:
    """超预算时可在完整的第二个工具轮起点切分。"""
    first_round = [AssistantMessage(tool_calls=[call("c1")]), tool("c1")]
    second_round = [AssistantMessage(tool_calls=[call("c2")]), tool("c2")]
    final = AssistantMessage(content="dddd")
    messages: list[Message] = [UserMessage(content="aaaa"), *first_round, *second_round, final]
    budget = tokens_of([*second_round, final])

    cut = find_cut_point(messages, keep_recent_tokens=budget)

    assert cut is not None
    assert cut.start_index == 3
    assert cut.boundary == "assistant"
    kept = messages[cut.start_index :]
    assert any(isinstance(message, ToolMessage) and message.tool_call_id == "c2" for message in kept)


def test_incomplete_batch_keeps_whole_turn() -> None:
    """工具结果缺失时，不能从该 assistant 切分，整轮一起保留。"""
    incomplete = AssistantMessage(tool_calls=[call("c1"), call("c2")])
    messages: list[Message] = [
        UserMessage(content="0000"),
        AssistantMessage(content="1111"),
        UserMessage(content="2222"),
        incomplete,
        tool("c1"),
    ]

    cut = find_cut_point(messages, keep_recent_tokens=1)

    assert cut == CutPoint(start_index=2, boundary="user", kept_tokens=tokens_of(messages[2:]))
    assert incomplete in messages[cut.start_index :] and messages[-1] in messages[cut.start_index :]


def test_partial_results_are_summarized_wholly() -> None:
    """残缺工具轮要么整轮保留、要么整轮进入摘要，绝不成为保留区间起点。"""
    messages: list[Message] = [
        UserMessage(content="0000"),
        AssistantMessage(tool_calls=[call("c1"), call("c2")]),
        tool("c1"),
        UserMessage(content="2222"),
    ]

    cut = find_cut_point(messages, keep_recent_tokens=1)

    assert cut == CutPoint(start_index=3, boundary="user", kept_tokens=1)
    assert messages[cut.start_index].role == "user"


def test_overlong_single_tool_result_is_kept_whole() -> None:
    """最近一轮含超长 tool result 时必须整体保留，切点回到该轮起点。"""
    batch = [AssistantMessage(tool_calls=[call("c1")]), tool("c1", content="x" * 4000)]
    messages: list[Message] = [UserMessage(content="aaaa"), *batch]

    cut = find_cut_point(messages, keep_recent_tokens=1)

    assert cut is not None
    assert cut.start_index == 1
    assert cut.kept_tokens == tokens_of(batch)


def test_cut_never_lands_on_tool_message() -> None:
    """遍历预算：任何切点都落在 user/assistant，且 assistant 的工具批次完整。"""
    messages: list[Message] = [
        UserMessage(content="0000"),
        AssistantMessage(tool_calls=[call("c1")]),
        tool("c1"),
        AssistantMessage(tool_calls=[call("c2"), call("c3")]),
        tool("c2"),
        tool("c3"),
        AssistantMessage(content="7777"),
        UserMessage(content="8888"),
        AssistantMessage(content="9999"),
    ]
    total = tokens_of(messages)

    for budget in range(1, total + 2):
        cut = find_cut_point(messages, keep_recent_tokens=budget)
        if cut is None:
            continue
        start_message = messages[cut.start_index]
        assert start_message.role in {"user", "assistant"}
        if isinstance(start_message, AssistantMessage) and start_message.tool_calls:
            expected = {item.id for item in start_message.tool_calls}
            assert expected <= result_ids_after(messages, cut.start_index)
        with pytest.raises(ValueError):
            find_cut_point(messages, keep_recent_tokens=0)
