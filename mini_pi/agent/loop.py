"""Agent Loop：驱动 LLM 与工具调用循环的纯函数。"""

from __future__ import annotations

from collections.abc import Callable

from mini_pi.agent.events import (
    AgentEndEvent,
    AgentEvent,
    AgentStartEvent,
    MessageDeltaEvent,
    MessageEndEvent,
    MessageStartEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
    TurnEndEvent,
    TurnStartEvent,
)
from mini_pi.agent.state import AgentState
from mini_pi.errors import MiniPiError, ToolError
from mini_pi.llm.base import LLMClient
from mini_pi.llm.types import (
    AssistantMessage,
    DoneEvent,
    ErrorEvent,
    Message,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolCall,
    ToolMessage,
)
from mini_pi.tools.base import ToolResult
from mini_pi.tools.registry import ToolRegistry

EventSink = Callable[[AgentEvent], None]

_TRUNCATED_MESSAGE = (
    "Tool call was truncated because the model reached the output token limit. "
    "Re-issue the call with complete arguments."
)


def run_loop(
    state: AgentState,
    llm: LLMClient,
    registry: ToolRegistry,
    *,
    max_steps: int = 50,
    on_event: EventSink | None = None,
) -> AssistantMessage:
    """执行 LLM → Tool → Observation 循环，返回最后一条 assistant 消息。"""
    if max_steps <= 0:
        raise ValueError("max_steps must be > 0")
    emit = on_event if on_event is not None else _noop
    emit(AgentStartEvent())
    last: AssistantMessage | None = None
    steps_this_run = 0
    while steps_this_run < max_steps:
        steps_this_run += 1
        state.step_count += 1
        step = state.step_count
        emit(TurnStartEvent(step=step))
        assistant = _stream_assistant(state, llm, registry, emit)
        last = assistant
        if assistant.stop_reason == "error":
            # 每轮 turn_start 都要有对应的 turn_end
            emit(TurnEndEvent(step=step))
            emit(AgentEndEvent(reason="error", message=assistant, error=assistant.error_message))
            return assistant
        if assistant.stop_reason == "length":
            # 输出被截断时 tool call 参数不完整，执行会产生脏操作
            _record_truncated_calls(state, assistant.tool_calls, emit)
            emit(TurnEndEvent(step=step))
            continue
        if not assistant.tool_calls:
            # 最终回答轮也要收尾，保证 turn_start / turn_end 成对
            emit(TurnEndEvent(step=step))
            emit(AgentEndEvent(reason="completed", message=assistant))
            return assistant
        _execute_tool_calls(state, registry, assistant.tool_calls, emit)
        emit(TurnEndEvent(step=step))
    # 循环由 max_steps 截断：保留最后消息供调用方检查
    assert last is not None
    emit(AgentEndEvent(reason="step_limit", message=last))
    return last


def _stream_assistant(
    state: AgentState,
    llm: LLMClient,
    registry: ToolRegistry,
    emit: EventSink,
) -> AssistantMessage:
    """消费一次流式回复：转发增量事件，结束后追加到 transcript。"""
    emit(MessageStartEvent())
    final: AssistantMessage | None = None
    for event in llm.stream(state.messages, registry.schemas()):
        if isinstance(event, TextDeltaEvent):
            emit(MessageDeltaEvent(kind="text", delta=event.delta))
        elif isinstance(event, ThinkingDeltaEvent):
            emit(MessageDeltaEvent(kind="thinking", delta=event.delta))
        elif isinstance(event, ErrorEvent):
            # 错误编码为 assistant 消息，由 run_loop 决定终止行为
            final = AssistantMessage(stop_reason="error", error_message=event.message)
        elif isinstance(event, DoneEvent):
            final = event.message
    if final is None:
        # 协议要求流必须以 done 结束；缺失说明 Client 实现有缺陷
        raise MiniPiError("LLM stream ended without a done event")
    state.messages.append(final)
    emit(MessageEndEvent(message=final))
    return final


def _execute_tool_calls(
    state: AgentState,
    registry: ToolRegistry,
    calls: list[ToolCall],
    emit: EventSink,
) -> None:
    """顺序执行工具调用；ToolError 转 observation，其他异常冒泡。"""
    for call in calls:
        emit(ToolExecutionStartEvent(tool_call=call))
        is_error = False
        try:
            result = registry.execute(call.name, call.arguments)
        except ToolError as exc:
            result = ToolResult(content=f"{type(exc).__name__}: {exc}")
            is_error = True
        # 只有工具显式声明 modified_files 时才记录，避免只读工具误入改动列表
        if not is_error and result.modified_files:
            state.modified_files.update(result.modified_files)
        _append_tool_message(state, call, result, is_error)
        emit(ToolExecutionEndEvent(tool_call=call, result=result, is_error=is_error))


def _record_truncated_calls(
    state: AgentState, calls: list[ToolCall], emit: EventSink
) -> None:
    """截断的 tool call 一律转 error observation，不进入工具执行。"""
    for call in calls:
        emit(ToolExecutionStartEvent(tool_call=call))
        result = ToolResult(content=_TRUNCATED_MESSAGE)
        _append_tool_message(state, call, result, is_error=True)
        emit(ToolExecutionEndEvent(tool_call=call, result=result, is_error=True))


def _append_tool_message(
    state: AgentState, call: ToolCall, result: ToolResult, is_error: bool
) -> None:
    message: Message = ToolMessage(
        tool_call_id=call.id,
        name=call.name,
        content=result.content,
        is_error=is_error,
    )
    state.messages.append(message)


def _noop(event: AgentEvent) -> None:
    pass
