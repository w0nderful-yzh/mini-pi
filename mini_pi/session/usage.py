"""从完整消息或 Session 活动链统计最近一次用户任务的模型用量与工具调用。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from mini_pi.llm.types import AssistantMessage, Message, ToolCall, ToolMessage, UserMessage
from mini_pi.session.models import MessageEntry, SessionEntry


@dataclass(frozen=True, slots=True)
class RunUsage:
    """一次用户任务的请求和工具计数；缺失 usage 不计入实测总数。"""

    requests: int
    measured_requests: int
    input_tokens: int
    output_tokens: int
    latest_input_tokens: int | None
    tool_calls: int
    duration_seconds: float | None


@dataclass(frozen=True, slots=True)
class RunToolCall:
    """最近一次任务里的一次工具调用：参数来自 assistant，结果来自 ToolMessage。"""

    call: ToolCall
    result: ToolMessage


def _summarize(messages: Sequence[Message], duration_seconds: float | None) -> RunUsage:
    """按完整 assistant 消息计请求，错误回复和步数截断也计入。"""
    assistants = [message for message in messages if isinstance(message, AssistantMessage)]
    measured = [message.usage for message in assistants if message.usage is not None]
    latest = assistants[-1].usage if assistants else None
    return RunUsage(
        requests=len(assistants),
        measured_requests=len(measured),
        input_tokens=sum(item.input_tokens for item in measured),
        output_tokens=sum(item.output_tokens for item in measured),
        latest_input_tokens=latest.input_tokens if latest is not None else None,
        tool_calls=sum(isinstance(message, ToolMessage) for message in messages),
        duration_seconds=duration_seconds,
    )


def _last_run_messages(messages: Sequence[Message]) -> list[Message]:
    """最后一条 user 消息之后的消息；没有 user 消息时返回空列表。"""
    for index in range(len(messages) - 1, -1, -1):
        if isinstance(messages[index], UserMessage):
            return list(messages[index + 1 :])
    return []


def _last_run_entries(
    entries: Sequence[SessionEntry],
) -> tuple[MessageEntry, list[MessageEntry]] | None:
    """返回（活动链上最后一条 user entry, 其后的 assistant/tool entry）；没有任务时 None。"""
    for index in range(len(entries) - 1, -1, -1):
        entry = entries[index]
        if isinstance(entry, MessageEntry) and isinstance(entry.message, UserMessage):
            tail = [
                item
                for item in entries[index + 1 :]
                if isinstance(item, MessageEntry)
                and isinstance(item.message, (AssistantMessage, ToolMessage))
            ]
            return entry, tail
    return None


def _pair_tools(messages: Sequence[Message]) -> tuple[RunToolCall, ...]:
    """按 tool_call_id 把 assistant 调用与结果配对；调用缺失时只用结果本身。"""
    calls: dict[str, ToolCall] = {}
    for message in messages:
        if isinstance(message, AssistantMessage):
            for call in message.tool_calls:
                calls[call.id] = call
    paired: list[RunToolCall] = []
    for message in messages:
        if isinstance(message, ToolMessage):
            paired.append(
                RunToolCall(
                    call=calls.get(message.tool_call_id)
                    or ToolCall(id=message.tool_call_id, name=message.name, arguments={}),
                    result=message,
                )
            )
    return tuple(paired)


def recent_run_usage(
    messages: Sequence[Message], *, duration_seconds: float | None = None
) -> RunUsage | None:
    """纯内存模式从最后一条真实 user 消息开始统计。"""
    tail = _last_run_messages(messages)
    return _summarize(tail, duration_seconds) if tail else None


def recent_run_tools(messages: Sequence[Message]) -> tuple[RunToolCall, ...]:
    """纯内存模式最近一次任务的工具调用；与 recent_run_usage 同一口径。"""
    return _pair_tools(_last_run_messages(messages))


def recent_session_run_usage(entries: Sequence[SessionEntry]) -> RunUsage | None:
    """从完整活动链重建任务，避免 compaction 投影和 UI 状态漏掉旧用量。"""
    found = _last_run_entries(entries)
    if found is None:
        return None
    user_entry, tail = found
    # JSONL 只有消息提交时间；记录跨度是耗时近似值，不冒充精确运行时间。
    duration = (
        max(0.0, (tail[-1].timestamp - user_entry.timestamp).total_seconds()) if tail else 0.0
    )
    return _summarize([item.message for item in tail], duration)


def recent_session_run_tools(entries: Sequence[SessionEntry]) -> tuple[RunToolCall, ...]:
    """从活动链重建最近一次任务的工具调用；details 不落盘，只按消息事实还原。"""
    found = _last_run_entries(entries)
    if found is None:
        return ()
    return _pair_tools([item.message for item in found[1]])
