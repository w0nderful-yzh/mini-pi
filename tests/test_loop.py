from __future__ import annotations

import pytest

from mini_pi.agent.events import TurnEndEvent
from mini_pi.agent.loop import run_loop
from mini_pi.agent.state import AgentState
from mini_pi.errors import LLMError
from mini_pi.llm.types import ToolMessage, UserMessage
from tests.conftest import FakeLLMClient, assistant, tool_call


def test_plain_answer_completes(echo_registry) -> None:
    """无工具调用时一轮结束，reason=completed。"""
    state = AgentState(messages=[UserMessage(content="hi")])
    llm = FakeLLMClient([assistant("hello")])
    result = run_loop(state, llm, echo_registry, on_event=None)
    assert result.content == "hello"
    assert state.step_count == 1
    assert state.messages[-1].content == "hello"


def test_tool_call_becomes_observation(echo_registry) -> None:
    """工具调用执行后以 ToolMessage 回传，并记录 modified_files。"""
    state = AgentState(messages=[UserMessage(content="echo it")])
    llm = FakeLLMClient(
        [
            assistant(tool_calls=[tool_call("c1", "echo", {"text": "hi"})]),
            assistant("done"),
        ]
    )
    result = run_loop(state, llm, echo_registry)
    assert result.content == "done"
    assert state.step_count == 2
    tool_messages = [message for message in state.messages if isinstance(message, ToolMessage)]
    assert len(tool_messages) == 1
    assert tool_messages[0].content == "hi"
    assert tool_messages[0].is_error is False
    assert state.modified_files == {"echo/hi.txt"}


def test_read_only_tool_does_not_mark_modified_files(echo_registry) -> None:
    """只读工具即使 details 带 path，也不得进入 modified_files。"""
    state = AgentState(messages=[UserMessage(content="inspect")])
    llm = FakeLLMClient(
        [
            assistant(tool_calls=[tool_call("c1", "inspect", {"path": "a.py"})]),
            assistant("done"),
        ]
    )
    run_loop(state, llm, echo_registry)
    assert state.modified_files == set()


def test_length_truncated_tool_calls_are_not_executed(echo_registry, events) -> None:
    """输出被 length 截断时 tool call 不可信：不执行，转 error observation 让模型重发。"""
    state = AgentState(messages=[UserMessage(content="long")])
    llm = FakeLLMClient(
        [
            assistant(
                tool_calls=[tool_call("c1", "echo", {"text": "partial"})],
                stop_reason="length",
            ),
            assistant("recovered"),
        ]
    )
    result = run_loop(state, llm, echo_registry, on_event=events.append)
    assert result.content == "recovered"
    tool_messages = [message for message in state.messages if isinstance(message, ToolMessage)]
    assert len(tool_messages) == 1
    assert tool_messages[0].is_error is True
    assert "truncated" in tool_messages[0].content
    assert state.modified_files == set()
    # 截断轮同样要 turn_start / turn_end 成对
    types = [event.type for event in events]
    assert types.count("turn_start") == types.count("turn_end") == 2


def test_llm_error_event_ends_agent(echo_registry, events) -> None:
    """LLM ErrorEvent 终止本轮：stop_reason=error，事件以 agent_end(error) 收尾。"""
    state = AgentState(messages=[UserMessage(content="hi")])
    llm = FakeLLMClient([LLMError("api down", retryable=False)])
    result = run_loop(state, llm, echo_registry, on_event=events.append)
    assert result.stop_reason == "error"
    assert result.error_message == "api down"
    assert state.step_count == 1
    assert events[-1].type == "agent_end"
    assert events[-1].reason == "error"
    assert events[-1].error == "api down"


def test_llm_error_message_is_in_transcript(echo_registry) -> None:
    """错误 assistant 消息也要落入 transcript，便于 UI 与后续 session 记录。"""
    state = AgentState(messages=[UserMessage(content="hi")])
    llm = FakeLLMClient([LLMError("api down", retryable=False)])
    run_loop(state, llm, echo_registry)
    assert state.messages[-1].stop_reason == "error"


def test_turn_end_is_emitted_for_final_answer(echo_registry, events) -> None:
    """最终回答轮也必须以 turn_end 收尾，保证 turn_start / turn_end 成对。"""
    state = AgentState(messages=[UserMessage(content="hi")])
    llm = FakeLLMClient([assistant("done")])
    run_loop(state, llm, echo_registry, on_event=events.append)
    types = [event.type for event in events]
    assert types.count("turn_start") == types.count("turn_end") == 1
    assert types[-2:] == ["turn_end", "agent_end"]


def test_event_sequence(echo_registry, events) -> None:
    """事件顺序稳定：agent_start → turn/message/tool 事件 → agent_end。"""
    state = AgentState(messages=[UserMessage(content="echo")])
    llm = FakeLLMClient(
        [
            assistant(tool_calls=[tool_call("c1", "echo", {"text": "x"})]),
            assistant("done"),
        ]
    )
    run_loop(state, llm, echo_registry, on_event=events.append)
    types = [event.type for event in events]
    assert types[0] == "agent_start"
    assert types[1:3] == ["turn_start", "message_start"]
    assert "message_delta" in types
    assert "tool_execution_start" in types
    assert "tool_execution_end" in types
    assert types[-1] == "agent_end"
    assert events[-1].reason == "completed"
    assert types.count("turn_start") == types.count("turn_end") == 2
    assert types[-2:] == ["turn_end", "agent_end"]
    assert any(isinstance(event, TurnEndEvent) for event in events)


def test_tool_error_becomes_error_observation(echo_registry) -> None:
    """ToolError 不中断循环，转成 is_error observation 让模型自我纠正。"""
    state = AgentState(messages=[UserMessage(content="fail")])
    llm = FakeLLMClient(
        [
            assistant(tool_calls=[tool_call("c1", "fail", {"reason": "nope"})]),
            assistant("recovered"),
        ]
    )
    result = run_loop(state, llm, echo_registry)
    assert result.content == "recovered"
    tool_message = [message for message in state.messages if isinstance(message, ToolMessage)][0]
    assert tool_message.is_error is True
    assert "nope" in tool_message.content


def test_unknown_tool_becomes_error_observation(echo_registry) -> None:
    """调用未注册工具同样转成 error observation。"""
    state = AgentState(messages=[UserMessage(content="unknown")])
    llm = FakeLLMClient(
        [
            assistant(tool_calls=[tool_call("c1", "nope", {})]),
            assistant("recovered"),
        ]
    )
    result = run_loop(state, llm, echo_registry)
    assert result.content == "recovered"
    tool_message = [message for message in state.messages if isinstance(message, ToolMessage)][0]
    assert tool_message.is_error is True
    assert "ToolNotFoundError" in tool_message.content


def test_invalid_arguments_become_error_observation(echo_registry) -> None:
    """参数校验失败也以 error observation 回传模型。"""
    state = AgentState(messages=[UserMessage(content="bad args")])
    llm = FakeLLMClient(
        [
            assistant(tool_calls=[tool_call("c1", "echo", {"wrong": 1})]),
            assistant("recovered"),
        ]
    )
    result = run_loop(state, llm, echo_registry)
    assert result.content == "recovered"
    tool_message = [message for message in state.messages if isinstance(message, ToolMessage)][0]
    assert tool_message.is_error is True
    assert "ToolArgumentError" in tool_message.content


def test_unexpected_tool_exception_propagates(echo_registry) -> None:
    """非 ToolError 异常是程序缺陷，必须冒泡而不是被吞掉。"""
    state = AgentState(messages=[UserMessage(content="crash")])
    llm = FakeLLMClient([assistant(tool_calls=[tool_call("c1", "crash", {})])])
    with pytest.raises(RuntimeError, match="boom"):
        run_loop(state, llm, echo_registry)


def test_step_limit_stops_loop(echo_registry, events) -> None:
    """达到 max_steps 后停止并报告 step_limit。"""
    state = AgentState(messages=[UserMessage(content="loop")])
    llm = FakeLLMClient(
        [
            assistant(tool_calls=[tool_call("c1", "echo", {"text": "1"})]),
            assistant(tool_calls=[tool_call("c2", "echo", {"text": "2"})]),
        ]
    )
    result = run_loop(state, llm, echo_registry, max_steps=2, on_event=events.append)
    assert state.step_count == 2
    assert result.tool_calls != []
    assert events[-1].reason == "step_limit"


def test_empty_user_message_is_allowed(echo_registry) -> None:
    """状态层不校验用户输入，空消息由 Agent 入口负责拦截。"""
    state = AgentState(messages=[UserMessage(content="")])
    llm = FakeLLMClient([assistant("ok")])
    assert run_loop(state, llm, echo_registry).content == "ok"


def test_max_steps_one_with_tool_call_reports_step_limit(echo_registry) -> None:
    """max_steps=1 且模型请求工具时，工具已执行但循环停止并如实计数。"""
    state = AgentState(messages=[UserMessage(content="once")])
    llm = FakeLLMClient([assistant(tool_calls=[tool_call("c1", "echo", {"text": "x"})])])
    result = run_loop(state, llm, echo_registry, max_steps=1)
    assert result.tool_calls
    assert state.step_count == 1
