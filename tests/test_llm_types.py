from __future__ import annotations

from pydantic import TypeAdapter

from mini_pi.llm.types import (
    AssistantMessage,
    DoneEvent,
    ErrorEvent,
    Message,
    StartEvent,
    StreamEvent,
    SystemMessage,
    TextDeltaEvent,
    ToolCall,
    ToolMessage,
    UserMessage,
)

# 判别联合类型需要 TypeAdapter 才能做批量校验与反序列化
MESSAGE_ADAPTER = TypeAdapter(list[Message])
STREAM_ADAPTER = TypeAdapter(list[StreamEvent])


def test_message_union_roundtrip() -> None:
    """消息按 role 判别序列化/反序列化，保证 transcript 可持久化与回放。"""
    messages: list[Message] = [
        SystemMessage(content="system"),
        UserMessage(content="hello"),
        AssistantMessage(
            content="",
            tool_calls=[ToolCall(id="c1", name="read_file", arguments={"path": "a.py"})],
            stop_reason="tool_calls",
        ),
        ToolMessage(tool_call_id="c1", name="read_file", content="file", is_error=False),
    ]
    dumped = [message.model_dump() for message in messages]
    restored = MESSAGE_ADAPTER.validate_python(dumped)
    assert [type(item) for item in restored] == [
        SystemMessage,
        UserMessage,
        AssistantMessage,
        ToolMessage,
    ]
    assert restored[2].tool_calls[0].arguments == {"path": "a.py"}


def test_assistant_defaults() -> None:
    """assistant 消息提供安全默认值，避免构造时强制填所有字段。"""
    message = AssistantMessage()
    assert message.content == ""
    assert message.tool_calls == []
    assert message.stop_reason == "stop"
    assert message.reasoning_content is None


def test_stream_event_discriminator() -> None:
    """流式事件按 type 判别，CLI/Loop 可安全分发。"""
    events = [
        StartEvent(),
        TextDeltaEvent(delta="hi"),
        DoneEvent(message=AssistantMessage(content="hi")),
        ErrorEvent(message="boom", retryable=True),
    ]
    dumped = [event.model_dump() for event in events]
    restored = STREAM_ADAPTER.validate_python(dumped)
    assert [event.type for event in restored] == ["start", "text_delta", "done", "error"]
