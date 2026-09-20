from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from pydantic import BaseModel

from mini_pi.errors import LLMError
from mini_pi.llm.types import (
    AssistantMessage,
    DoneEvent,
    ErrorEvent,
    Message,
    StartEvent,
    StreamEvent,
    TextDeltaEvent,
    ToolCall,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    ToolSchema,
)
from mini_pi.tools.base import Tool, ToolResult
from mini_pi.tools.registry import ToolRegistry


def assistant(
    content: str = "",
    *,
    tool_calls: list[ToolCall] | None = None,
    stop_reason: str | None = None,
    reasoning_content: str | None = None,
) -> AssistantMessage:
    """构造脚本化 assistant 消息，自动推断默认 stop_reason。"""
    calls = tool_calls or []
    if stop_reason is None:
        stop_reason = "tool_calls" if calls else "stop"
    return AssistantMessage(
        content=content,
        reasoning_content=reasoning_content,
        tool_calls=calls,
        stop_reason=stop_reason,
    )


def tool_call(call_id: str, name: str, arguments: dict[str, Any]) -> ToolCall:
    """构造工具调用。"""
    return ToolCall(id=call_id, name=name, arguments=arguments)


class FakeLLMClient:
    """脚本化假客户端：按顺序回放 assistant 消息或 LLMError，并记录收到的上下文。"""

    def __init__(self, script: list[AssistantMessage | LLMError]) -> None:
        self._script = list(script)
        self.calls: list[list[Message]] = []
        self.tools_seen: list[list[ToolSchema] | None] = []

    def stream(
        self, messages: list[Message], tools: list[ToolSchema] | None = None
    ) -> Iterator[StreamEvent]:
        self.calls.append(list(messages))
        self.tools_seen.append(tools)
        if not self._script:
            raise AssertionError("FakeLLMClient script exhausted")
        item = self._script.pop(0)
        if isinstance(item, LLMError):
            yield ErrorEvent(message=str(item), retryable=item.retryable)
            return
        # 将完整消息拆成事件流，模拟真实流式行为
        yield StartEvent()
        if item.content:
            yield TextDeltaEvent(delta=item.content)
        for index, call in enumerate(item.tool_calls):
            yield ToolCallStartEvent(index=index, id=call.id, name=call.name)
            yield ToolCallDeltaEvent(index=index, arguments_delta=json.dumps(call.arguments))
            yield ToolCallEndEvent(index=index, tool_call=call)
        yield DoneEvent(message=item)

    def complete(
        self, messages: list[Message], tools: list[ToolSchema] | None = None
    ) -> AssistantMessage:
        for event in self.stream(messages, tools):
            if isinstance(event, DoneEvent):
                return event.message
            if isinstance(event, ErrorEvent):
                raise LLMError(event.message, retryable=event.retryable)
        raise AssertionError("unreachable")


class EchoArgs(BaseModel):
    text: str


class EchoTool(Tool):
    """回显工具，details 带 path 用于验证 modified_files 追踪。"""

    name = "echo"
    description = "Echo the input text."
    args_model = EchoArgs

    def execute(self, text: str) -> ToolResult:
        return ToolResult(content=text, details={"path": f"echo/{text}.txt"})


class FailingArgs(BaseModel):
    reason: str


class FailingTool(Tool):
    """可预期失败：抛 ToolError，应转成 error observation。"""

    name = "fail"
    description = "Always raises ToolError."
    args_model = FailingArgs

    def execute(self, reason: str) -> ToolResult:
        from mini_pi.errors import ToolError

        raise ToolError(reason)


class CrashArgs(BaseModel):
    pass


class CrashTool(Tool):
    """程序缺陷：抛非 ToolError 异常，应直接冒泡。"""

    name = "crash"
    description = "Raises an unexpected exception."
    args_model = CrashArgs

    def execute(self) -> ToolResult:
        raise RuntimeError("boom")


@pytest.fixture
def events() -> list:
    """事件收集器，替代 CLI 作为 on_event 消费者。"""
    return []


@pytest.fixture
def echo_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(EchoTool())
    registry.register(FailingTool())
    registry.register(CrashTool())
    return registry
