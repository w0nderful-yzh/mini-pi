from __future__ import annotations

from pydantic import TypeAdapter

from mini_pi.agent.events import (
    AgentEndEvent,
    AgentEvent,
    AgentStartEvent,
    MessageDeltaEvent,
    MessageEndEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
    TurnEndEvent,
    TurnStartEvent,
)
from mini_pi.agent.state import AgentState
from mini_pi.llm.types import AssistantMessage, ToolCall, UserMessage
from mini_pi.tools.base import ToolResult

# 判别联合需要 TypeAdapter 才能批量校验事件序列
EVENT_ADAPTER = TypeAdapter(list[AgentEvent])


def test_state_defaults_and_mutation() -> None:
    """AgentState 默认空消息、零步数、空文件集合，且可变。"""
    state = AgentState()
    assert state.messages == []
    assert state.step_count == 0
    assert state.modified_files == set()
    state.modified_files.add("a.py")
    assert state.modified_files == {"a.py"}


def test_event_union_roundtrip() -> None:
    """九类 AgentEvent 按 type 判别序列化/反序列化，事件序列可录制回放。"""
    call = ToolCall(id="c1", name="echo", arguments={"text": "hi"})
    events = [
        AgentStartEvent(),
        TurnStartEvent(step=1),
        MessageDeltaEvent(kind="text", delta="hi"),
        MessageEndEvent(message=AssistantMessage(content="hi")),
        ToolExecutionStartEvent(tool_call=call),
        ToolExecutionEndEvent(tool_call=call, result=ToolResult(content="hi"), is_error=False),
        TurnEndEvent(step=1),
        AgentEndEvent(reason="completed", message=AssistantMessage(content="done")),
    ]
    dumped = [event.model_dump() for event in events]
    restored = EVENT_ADAPTER.validate_python(dumped)
    assert [event.type for event in restored] == [
        "agent_start",
        "turn_start",
        "message_delta",
        "message_end",
        "tool_execution_start",
        "tool_execution_end",
        "turn_end",
        "agent_end",
    ]
    assert restored[5].result.content == "hi"


def test_state_accepts_messages() -> None:
    """state 可以直接用历史消息初始化，便于测试与后续 resume。"""
    state = AgentState(messages=[UserMessage(content="hi")])
    assert state.messages[0].content == "hi"
