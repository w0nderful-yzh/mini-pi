"""活动分支投影：把 Session entry 树还原为 root → leaf 的有序路径。"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from mini_pi.context.sections import SystemPromptState, replay_system_messages
from mini_pi.errors import SessionError
from mini_pi.llm.types import AssistantMessage, Message, SystemMessage, ToolMessage

if TYPE_CHECKING:
    # 仅用于类型标注：运行时导入会触发 session 包与 jsonl 的循环依赖。
    from mini_pi.session.models import SessionEntry


def project_entry_path(
    entries: Iterable[SessionEntry],
    *,
    leaf_id: UUID | None,
) -> tuple[SessionEntry, ...]:
    """沿 leaf 的 parent 链投影 entry 路径；纯函数，不修改也不复制入参。

    - `leaf_id is None`（空会话）返回空路径
    - 未知 leaf、孤儿 parent、parent 环、重复 id 都属于损坏状态，明确失败
    """
    index = _index_entries(entries)
    if leaf_id is None:
        return ()
    if leaf_id not in index:
        raise SessionError(f"unknown leaf in session: {leaf_id}")

    path: list[SessionEntry] = []
    seen: set[UUID] = set()
    current_id: UUID | None = leaf_id
    while current_id is not None:
        if current_id in seen:
            raise SessionError(f"cycle in session parent chain: {current_id}")
        seen.add(current_id)
        entry = index.get(current_id)
        if entry is None:
            raise SessionError(f"unknown parentId in session: {current_id}")
        path.append(entry)
        current_id = entry.parent_id
    # 回溯得到 leaf→root，反转成调用方需要的 root→leaf。
    return tuple(reversed(path))


def _index_entries(entries: Iterable[SessionEntry]) -> dict[UUID, SessionEntry]:
    """建立 id 索引；重复 id 会让路径有歧义，按损坏状态处理。"""
    index: dict[UUID, SessionEntry] = {}
    for entry in entries:
        if entry.id in index:
            raise SessionError(f"duplicate entry id in session: {entry.id}")
        index[entry.id] = entry
    return index


@dataclass(frozen=True, slots=True)
class MessageProjection:
    """message entry path 的模型消息视图与结构化 system prompt 状态。"""

    messages: tuple[Message, ...]
    system_prompt: SystemPromptState | None


def project_messages(entries: Iterable[SessionEntry]) -> MessageProjection:
    """把 message entry path 还原为模型 messages 与结构化 system prompt 状态。

    - 只处理 message entry；compaction 的切点语义属于 M7.4c，遇到即报错。
    - assistant tool_calls 与 tool result 必须完整、唯一配对，否则 Fail Fast。
    """
    # 运行时导入避免 session 包初始化期间与 jsonl 形成循环依赖
    from mini_pi.session.models import CompactionEntry

    messages: list[Message] = []
    system_messages: list[SystemMessage] = []
    for entry in entries:
        if isinstance(entry, CompactionEntry):
            raise SessionError("compaction entry requires M7.4c projection")
        messages.append(entry.message)
        if isinstance(entry.message, SystemMessage):
            system_messages.append(entry.message)

    _validate_tool_pairs(messages)
    try:
        system_prompt = replay_system_messages(system_messages)
    except ValueError as exc:
        # 结构化 system 历史的协议错误归一到 Session 域错误
        raise SessionError(f"invalid system prompt history: {exc}") from exc
    return MessageProjection(messages=tuple(messages), system_prompt=system_prompt)


def _validate_tool_pairs(messages: Sequence[Message]) -> None:
    """保证每个 tool call 恰好有一个紧随其结果，且 id 全局唯一。"""
    seen_call_ids: set[str] = set()
    pending: set[str] = set()
    for message in messages:
        if isinstance(message, AssistantMessage):
            if pending:
                raise SessionError(f"missing tool result for tool_call_id(s): {_sorted(pending)}")
            for call in message.tool_calls:
                if call.id in seen_call_ids:
                    raise SessionError(f"duplicate tool_call id in transcript: {call.id}")
                seen_call_ids.add(call.id)
                pending.add(call.id)
            continue
        if isinstance(message, ToolMessage):
            if message.tool_call_id not in pending:
                raise SessionError(
                    f"tool result without matching tool call: {message.tool_call_id}"
                )
            pending.discard(message.tool_call_id)
            continue
        if pending:
            # user / system 消息插入意味着上一轮工具批次永远凑不齐
            raise SessionError(f"missing tool result for tool_call_id(s): {_sorted(pending)}")
    if pending:
        raise SessionError(f"missing tool result for tool_call_id(s): {_sorted(pending)}")


def _sorted(ids: set[str]) -> list[str]:
    """错误信息按 id 排序，保证测试与日志稳定。"""
    return sorted(ids)
