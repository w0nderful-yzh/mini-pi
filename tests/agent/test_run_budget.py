"""单次任务累计输入预算在模型请求边界安全停止。"""

from __future__ import annotations

import pytest

from mini_pi.agent.events import AgentEvent, AgentEndEvent, BudgetWarningEvent
from mini_pi.agent.loop import run_loop
from mini_pi.agent.state import AgentState
from mini_pi.context.tokens import estimate_tokens
from mini_pi.llm.types import AssistantMessage, ToolMessage, Usage, UserMessage
from mini_pi.tools.registry import ToolRegistry
from tests.conftest import EchoTool, FakeLLMClient, assistant, tool_call


def _usage_reply(
    input_tokens: int,
    *,
    output_tokens: int = 0,
    content: str = "",
    calls: list | None = None,
) -> AssistantMessage:
    """构造带 Provider 实测 input 的回复。"""
    message = assistant(content, tool_calls=calls)
    return message.model_copy(
        update={
            "usage": Usage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
            )
        }
    )


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(EchoTool())
    return registry


def test_budget_warns_once_with_transient_prompt_then_allows_conclusion() -> None:
    """接近上限时模型得到一次未持久化提示，可直接作答完成。"""
    state = AgentState(messages=[UserMessage(content="task")])
    llm = FakeLLMClient(
        [
            _usage_reply(100, calls=[tool_call("c1", "echo", {"text": "fact"})]),
            _usage_reply(180, content="done"),
        ]
    )
    events: list[AgentEvent] = []

    result = run_loop(
        state,
        llm,
        _registry(),
        max_run_input_tokens=300,
        on_event=events.append,
    )

    assert result.content == "done"
    warnings = [event for event in events if isinstance(event, BudgetWarningEvent)]
    assert len(warnings) == 1
    assert warnings[0].source == "provider"
    assert "<runtime_budget_notice>" in llm.calls[1][-1].content
    assert not any(
        isinstance(message, UserMessage) and "runtime_budget_notice" in message.content
        for message in state.messages
    )
    assert isinstance(events[-1], AgentEndEvent)
    assert events[-1].reason == "completed"


def test_budget_stops_after_complete_tool_batch_without_replaying_tools() -> None:
    """预计下一请求超限时保留第二批工具结果，不发第三次请求。"""
    state = AgentState(messages=[UserMessage(content="task")])
    llm = FakeLLMClient(
        [
            _usage_reply(100, calls=[tool_call("c1", "echo", {"text": "one"})]),
            _usage_reply(180, calls=[tool_call("c2", "echo", {"text": "two"})]),
            _usage_reply(200, content="unused"),
        ]
    )
    events: list[AgentEvent] = []

    result = run_loop(
        state,
        llm,
        _registry(),
        max_run_input_tokens=300,
        on_event=events.append,
    )

    assert result.tool_calls[0].id == "c2"
    assert len(llm.calls) == 2
    assert [message.role for message in state.messages] == [
        "user", "assistant", "tool", "assistant", "tool"
    ]
    assert [message.content for message in state.messages if isinstance(message, ToolMessage)] == [
        "one", "two"
    ]
    end = events[-1]
    assert isinstance(end, AgentEndEvent)
    assert end.reason == "budget_limit"
    assert (end.budget_limit, end.budget_used, end.budget_source) == (300, 280, "provider")
    assert end.predicted_next_input is not None


def test_budget_can_block_first_request_from_projection() -> None:
    """首请求预计超限时不调用模型，并将来源标为 estimated。"""
    state = AgentState(messages=[UserMessage(content="x" * 80)])
    llm = FakeLLMClient([assistant("unused")])
    events: list[AgentEvent] = []

    result = run_loop(
        state,
        llm,
        _registry(),
        max_run_input_tokens=1,
        on_event=events.append,
    )

    assert result.stop_reason == "error"
    assert llm.calls == []
    assert [message.role for message in state.messages] == ["user"]
    end = events[-1]
    assert isinstance(end, AgentEndEvent)
    assert end.reason == "budget_limit"
    assert end.budget_source == "estimated"


def test_missing_provider_usage_counts_request_projection_as_estimated() -> None:
    """响应缺 usage 时累计请求前投影，下一边界仍能明确停止。"""
    state = AgentState(messages=[UserMessage(content="task")])
    first_projection = estimate_tokens(state.messages).tokens
    llm = FakeLLMClient(
        [assistant(tool_calls=[tool_call("c1", "echo", {"text": "fact"})])]
    )
    events: list[AgentEvent] = []

    run_loop(
        state,
        llm,
        _registry(),
        max_run_input_tokens=first_projection + 1,
        on_event=events.append,
    )

    assert len(llm.calls) == 1
    end = events[-1]
    assert isinstance(end, AgentEndEvent)
    assert end.reason == "budget_limit"
    assert end.budget_used == first_projection
    assert end.budget_source == "estimated"


def test_default_budget_is_disabled_and_invalid_budget_fails_fast() -> None:
    """默认行为不增加警告；非正预算在任何模型请求前拒绝。"""
    state = AgentState(messages=[UserMessage(content="task")])
    events: list[AgentEvent] = []
    run_loop(state, FakeLLMClient([assistant("done")]), _registry(), on_event=events.append)
    assert not any(isinstance(event, BudgetWarningEvent) for event in events)
    assert isinstance(events[-1], AgentEndEvent) and events[-1].reason == "completed"

    with pytest.raises(ValueError, match="max_run_input_tokens must be > 0"):
        run_loop(
            AgentState(messages=[UserMessage(content="task")]),
            FakeLLMClient([]),
            _registry(),
            max_run_input_tokens=0,
        )
