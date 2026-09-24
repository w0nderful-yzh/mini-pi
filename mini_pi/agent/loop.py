"""Agent Loop：驱动 LLM 与工具调用循环的纯函数。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from mini_pi.agent.events import (
    AgentEndEvent,
    AgentEvent,
    AgentStartEvent,
    BudgetWarningEvent,
    MessageDeltaEvent,
    MessageEndEvent,
    MessageStartEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
    TurnEndEvent,
    TurnStartEvent,
)
from mini_pi.agent.state import (
    AgentState,
    MessageCommit,
    PrepareNextTurn,
    commit_message,
)
from mini_pi.context.tokens import estimate_tokens
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
    UserMessage,
)
from mini_pi.tools.base import ToolResult
from mini_pi.tools.registry import ToolRegistry

EventSink = Callable[[AgentEvent], None]

_TRUNCATED_MESSAGE = (
    "Tool call was truncated because the model reached the output token limit. "
    "Re-issue the call with complete arguments."
)

_CANCELLED_MESSAGE = (
    "Tool execution was cancelled by the user (Ctrl+C). "
    "The command may have been terminated; re-run it if the result is still needed."
)

BudgetSource = Literal["provider", "estimated", "mixed"]


@dataclass(slots=True)
class _RunInputBudget:
    """一次 run 的累计输入预算；实际 usage 缺失时按请求前投影估算。"""

    limit: int
    used: int = 0
    measured_requests: int = 0
    estimated_requests: int = 0
    warned: bool = False

    @property
    def source(self) -> BudgetSource:
        """本run已用量的来源：全实测 provider、全估算，或两者 mixed。"""
        if self.measured_requests and self.estimated_requests:
            return "mixed"
        if self.measured_requests:
            return "provider"
        return "estimated"

    def record(self, assistant: AssistantMessage, predicted_input: int) -> None:
        """Provider input 优先；缺失时用请求发出前的可复现估算。"""
        if assistant.usage is not None:
            self.used += assistant.usage.input_tokens
            self.measured_requests += 1
        else:
            self.used += predicted_input
            self.estimated_requests += 1


def _budget_notice(budget: _RunInputBudget, predicted_input: int) -> UserMessage:
    """构造不持久化的运行时提示，让模型有一次基于现有证据收敛的机会。"""
    remaining = max(0, budget.limit - budget.used)
    return UserMessage(
        content=(
            "<runtime_budget_notice>\n"
            f"This run has {remaining} input tokens remaining before the configured "
            f"request-boundary budget of {budget.limit}. The next request is estimated "
            f"at {predicted_input} input tokens. Answer from the evidence already gathered "
            "when possible. Call another tool only if it is necessary; a later request may "
            "be blocked.\n"
            "</runtime_budget_notice>"
        )
    )


def _emit_budget_limit(
    emit: EventSink,
    budget: _RunInputBudget,
    predicted_input: int,
    last: AssistantMessage | None,
) -> AssistantMessage:
    """在请求前停止；不追加假 assistant，也不破坏刚提交的工具结果。"""
    emit(
        AgentEndEvent(
            reason="budget_limit",
            message=last,
            budget_limit=budget.limit,
            budget_used=budget.used,
            predicted_next_input=predicted_input,
            budget_source=budget.source,
        )
    )
    return last or AssistantMessage(
        stop_reason="error",
        error_message="run input budget blocked the first model request",
    )


def run_loop(
    state: AgentState,
    llm: LLMClient,
    registry: ToolRegistry,
    *,
    max_steps: int = 50,
    max_run_input_tokens: int | None = None,
    on_event: EventSink | None = None,
    on_message_commit: MessageCommit | None = None,
    prepare_next_turn: PrepareNextTurn | None = None,
) -> AssistantMessage:
    """执行 LLM → Tool → Observation 循环，返回最后一条 assistant 消息。

    `prepare_next_turn` 只在完整工具批次提交后、下一次模型请求前调用；它可以替换
    `state.messages`（例如压缩），`None` 时保持 Phase 1 的事件行为不变。截断轮
    （stop_reason=length）没有真实工具批次，不触发该钩子。钩子抛出的 `MiniPiError`
    是可预期失败（例如自动压缩没能把投影降到阈值内）：以 agent error 结束本次 run，
    且不发送下一次请求；其他异常是程序缺陷，直接冒泡。

    用户中断（KeyboardInterrupt）是可预期的终止：流式轮尚未提交的 assistant 不补写，
    工具轮已发出的 tool_call 会补 cancelled observation 保持配对，已提交的消息与
    文件改动全部保留，并以 `AgentEndEvent(reason="cancelled")` 结束，绝不伪装成 completed。
    """
    if max_steps <= 0:
        raise ValueError("max_steps must be > 0")
    if max_run_input_tokens is not None and max_run_input_tokens <= 0:
        raise ValueError("max_run_input_tokens must be > 0")
    emit = on_event if on_event is not None else _noop
    emit(AgentStartEvent())
    budget = (
        _RunInputBudget(limit=max_run_input_tokens)
        if max_run_input_tokens is not None
        else None
    )
    last: AssistantMessage | None = None
    steps_this_run = 0
    while steps_this_run < max_steps:
        request_messages = state.messages
        predicted_input = estimate_tokens(request_messages).tokens
        if budget is not None:
            if budget.used + predicted_input > budget.limit:
                return _emit_budget_limit(emit, budget, predicted_input, last)
            if (
                last is not None
                and not budget.warned
                and budget.used + 2 * predicted_input > budget.limit
            ):
                notice = _budget_notice(budget, predicted_input)
                candidate = [*state.messages, notice]
                candidate_input = estimate_tokens(candidate).tokens
                if budget.used + candidate_input > budget.limit:
                    return _emit_budget_limit(emit, budget, candidate_input, last)
                budget.warned = True
                request_messages = candidate
                predicted_input = candidate_input
                emit(
                    BudgetWarningEvent(
                        limit=budget.limit,
                        used=budget.used,
                        remaining=max(0, budget.limit - budget.used),
                        predicted_next_input=predicted_input,
                        source=budget.source,
                    )
                )
        steps_this_run += 1
        state.step_count += 1
        step = state.step_count
        emit(TurnStartEvent(step=step))
        try:
            assistant = _stream_assistant(
                state,
                llm,
                registry,
                emit,
                on_message_commit,
                request_messages=request_messages,
            )
        except KeyboardInterrupt:
            # 流已开始但完整消息尚未提交：不追加假 assistant，只按已发生的事实收尾
            emit(TurnEndEvent(step=step))
            emit(AgentEndEvent(reason="cancelled", message=last))
            return _cancelled_message()
        if budget is not None:
            budget.record(assistant, predicted_input)
        last = assistant
        if assistant.stop_reason == "error":
            # 每轮 turn_start 都要有对应的 turn_end
            emit(TurnEndEvent(step=step))
            emit(AgentEndEvent(reason="error", message=assistant, error=assistant.error_message))
            return assistant
        if assistant.stop_reason == "length":
            # 输出被截断时 tool call 参数不完整，执行会产生脏操作
            _record_truncated_calls(state, assistant.tool_calls, emit, on_message_commit)
            emit(TurnEndEvent(step=step))
            continue
        if not assistant.tool_calls:
            # 最终回答轮也要收尾，保证 turn_start / turn_end 成对
            emit(TurnEndEvent(step=step))
            emit(AgentEndEvent(reason="completed", message=assistant))
            return assistant
        cancelled = _execute_tool_calls(
            state, registry, assistant.tool_calls, emit, on_message_commit
        )
        emit(TurnEndEvent(step=step))
        if cancelled:
            # 当前与剩余 tool call 已补齐 cancelled observation，配对完整后结束本次 run
            emit(AgentEndEvent(reason="cancelled", message=assistant))
            return _cancelled_message()
        if prepare_next_turn is not None:
            try:
                # 工具批次已提交、turn 已收尾：下一次请求会重新读取 state.messages
                prepare_next_turn()
            except MiniPiError as exc:
                # 可预期失败（如自动压缩无法把投影降到阈值内）：不追加假 assistant、
                # 不改投影，以 agent error 结束，避免发出已经越界的下一次请求
                stopped = AssistantMessage(stop_reason="error", error_message=str(exc))
                emit(AgentEndEvent(reason="error", message=stopped, error=str(exc)))
                return stopped
            except KeyboardInterrupt:
                # 钩子（如摘要压缩的模型调用）被中断：事务未写盘，投影保持原样
                emit(AgentEndEvent(reason="cancelled", message=assistant))
                return _cancelled_message()
    # 循环由 max_steps 截断：保留最后消息供调用方检查
    assert last is not None
    emit(AgentEndEvent(reason="step_limit", message=last))
    return last


def _stream_assistant(
    state: AgentState,
    llm: LLMClient,
    registry: ToolRegistry,
    emit: EventSink,
    on_message_commit: MessageCommit | None,
    *,
    request_messages: list[Message] | None = None,
) -> AssistantMessage:
    """消费一次流式回复：转发增量事件，完整消息提交后加入 transcript。"""
    emit(MessageStartEvent())
    final: AssistantMessage | None = None
    for event in llm.stream(
        state.messages if request_messages is None else request_messages,
        registry.schemas(),
    ):
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
    commit_message(state, final, on_message_commit)
    emit(MessageEndEvent(message=final))
    return final


def _cancelled_message() -> AssistantMessage:
    """合成取消标记；它只用于表达本次 run 被中断，不作为模型消息提交。"""
    return AssistantMessage(stop_reason="cancelled")


def _execute_tool_calls(
    state: AgentState,
    registry: ToolRegistry,
    calls: list[ToolCall],
    emit: EventSink,
    on_message_commit: MessageCommit | None,
) -> bool:
    """顺序执行工具调用；ToolError 转 observation，用户中断补齐配对后返回 True。"""
    for index, call in enumerate(calls):
        emit(ToolExecutionStartEvent(tool_call=call))
        is_error = False
        try:
            result = registry.execute(call.name, call.arguments)
        except ToolError as exc:
            result = ToolResult(content=f"{type(exc).__name__}: {exc}")
            is_error = True
        except KeyboardInterrupt:
            # 中断发生在当前调用内部：先补当前结果，再补齐剩余调用，
            # 保证 assistant tool_calls 与 ToolMessage 一一配对后才结束这次 run
            _append_cancelled_calls(state, calls[index:], emit, on_message_commit)
            return True
        _append_tool_message(state, call, result, is_error, on_message_commit)
        emit(ToolExecutionEndEvent(tool_call=call, result=result, is_error=is_error))
    return False


def _append_cancelled_calls(
    state: AgentState,
    calls: list[ToolCall],
    emit: EventSink,
    on_message_commit: MessageCommit | None,
) -> None:
    """为被中断的当前调用及其余调用补 cancelled observation。

    调用方已发出第一个 tool_execution_start，这里只补它的 end 与后续调用的 start/end，
    避免出现只有 tool_call 没有 tool_result 的历史。
    """
    for offset, call in enumerate(calls):
        if offset > 0:
            emit(ToolExecutionStartEvent(tool_call=call))
        result = ToolResult(content=_CANCELLED_MESSAGE)
        _append_tool_message(
            state, call, result, is_error=True, on_message_commit=on_message_commit
        )
        emit(ToolExecutionEndEvent(tool_call=call, result=result, is_error=True))


def _record_truncated_calls(
    state: AgentState,
    calls: list[ToolCall],
    emit: EventSink,
    on_message_commit: MessageCommit | None,
) -> None:
    """截断的 tool call 一律转 error observation，不进入工具执行。"""
    for call in calls:
        emit(ToolExecutionStartEvent(tool_call=call))
        result = ToolResult(content=_TRUNCATED_MESSAGE)
        _append_tool_message(
            state, call, result, is_error=True, on_message_commit=on_message_commit
        )
        emit(ToolExecutionEndEvent(tool_call=call, result=result, is_error=True))


def _append_tool_message(
    state: AgentState,
    call: ToolCall,
    result: ToolResult,
    is_error: bool,
    on_message_commit: MessageCommit | None,
) -> None:
    """将工具结果组装为 ToolMessage 并提交；失败时不记录 modified_files。"""
    message = ToolMessage(
        tool_call_id=call.id,
        name=call.name,
        content=result.content,
        is_error=is_error,
        modified_files=result.modified_files if not is_error else [],
    )
    commit_message(state, message, on_message_commit)
    # 改动集合只反映已提交的 observation；写盘失败不能留下旁路内存状态。
    state.modified_files.update(message.modified_files)


def _noop(event: AgentEvent) -> None:
    """默认事件 Sink：吞掉事件，供不需展示的调用方（如测试）使用。"""
    pass
