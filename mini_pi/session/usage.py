"""从完整消息或 Session 活动链统计最近一次用户任务的模型用量。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from mini_pi.llm.types import AssistantMessage, Message, ToolMessage, UserMessage
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


def recent_run_usage(
    messages: Sequence[Message], *, duration_seconds: float | None = None
) -> RunUsage | None:
    """纯内存模式从最后一条真实 user 消息开始统计。"""
    for index in range(len(messages) - 1, -1, -1):
        if isinstance(messages[index], UserMessage):
            return _summarize(messages[index + 1 :], duration_seconds)
    return None


def recent_session_run_usage(entries: Sequence[SessionEntry]) -> RunUsage | None:
    """从完整活动链重建任务，避免 compaction 投影和 UI 状态漏掉旧用量。"""
    for index in range(len(entries) - 1, -1, -1):
        entry = entries[index]
        if isinstance(entry, MessageEntry) and isinstance(entry.message, UserMessage):
            tail = [
                item for item in entries[index + 1 :]
                if isinstance(item, MessageEntry)
                and isinstance(item.message, (AssistantMessage, ToolMessage))
            ]
            # JSONL 只有消息提交时间；记录跨度是耗时近似值，不冒充精确运行时间。
            duration = (
                max(0.0, (tail[-1].timestamp - entry.timestamp).total_seconds())
                if tail
                else 0.0
            )
            return _summarize([item.message for item in tail], duration)
    return None
