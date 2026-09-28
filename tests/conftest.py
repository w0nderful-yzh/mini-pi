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
    """回显工具，显式声明 modified_files 用于验证改动追踪。"""

    name = "echo"
    description = "Echo the input text."
    args_model = EchoArgs

    def execute(self, text: str) -> ToolResult:
        return ToolResult(
            content=text,
            details={"echoed": text},
            modified_files=[f"echo/{text}.txt"],
        )


class InspectArgs(BaseModel):
    path: str


class InspectTool(Tool):
    """只读工具：details 带 path，但不得声明 modified_files。"""

    name = "inspect"
    description = "Read-only inspection."
    args_model = InspectArgs

    def execute(self, path: str) -> ToolResult:
        return ToolResult(content=f"contents of {path}", details={"path": path})


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


class ScriptedReader:
    """脚本化 reader：按序返回文本；元素是异常时抛出，用于模拟 Ctrl+C 或 EOF。"""

    def __init__(self, items: list[object]) -> None:
        """保存脚本项；读完返回 EOFError，与真实 reader 的结束语义一致。"""
        self._items = list(items)
        self.reads = 0
        # 满足 ReplReader 契约：脚本化输入没有降级原因
        self.notice: str | None = None

    def read(self) -> str:
        """返回下一项文本或抛出脚本化异常。"""
        if not self._items:
            raise EOFError
        self.reads += 1
        item = self._items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return str(item)


class InterruptingStreamLLM:
    """流式阶段抛 KeyboardInterrupt，模拟任务执行中的 Ctrl+C。"""

    def __init__(self) -> None:
        """记录调用次数，便于断言取消后不再请求。"""
        self.calls: list[list[Message]] = []

    def stream(
        self, messages: list[Message], tools: list[ToolSchema] | None = None
    ) -> Iterator[StreamEvent]:
        """产出一次增量后中断，确认半截内容不进入 Session。"""
        self.calls.append(list(messages))
        yield TextDeltaEvent(delta="partial")
        raise KeyboardInterrupt

    def complete(
        self, messages: list[Message], tools: list[ToolSchema] | None = None
    ) -> AssistantMessage:
        """取消场景不提供 complete。"""
        raise AssertionError("complete must not be used")


@pytest.fixture
def echo_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(EchoTool())
    registry.register(InspectTool())
    registry.register(FailingTool())
    registry.register(CrashTool())
    return registry
