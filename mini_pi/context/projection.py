"""活动分支投影：把 Session entry 树还原为 root → leaf 的有序路径。"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from mini_pi.context.sections import SystemPromptState, replay_system_messages
from mini_pi.errors import SessionError
from mini_pi.llm.types import (
    AssistantMessage,
    Message,
    SystemMessage,
    ToolMessage,
    UserMessage,
)

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


# 摘要以显式标签包装为 user 级上下文，避免被模型当作 system 指令
SUMMARY_TAG = "compacted-conversation-summary"


@dataclass(frozen=True, slots=True)
class CompactionProjection:
    """活动路径上最近一次 compaction 生效后的上下文投影。"""

    system_prompt: SystemPromptState
    summary_message: UserMessage
    kept_messages: tuple[Message, ...]

    @property
    def messages(self) -> tuple[Message, ...]:
        """system 快照 → 摘要 → 保留消息，顺序可直接替换 AgentState.messages。"""
        return (
            _system_message_from_state(self.system_prompt),
            self.summary_message,
            *self.kept_messages,
        )


def project_compaction(entries: Iterable[SessionEntry]) -> CompactionProjection | None:
    """用活动路径上最新的 compaction 投影上下文；没有任何 compaction 返回 None。

    - 重复压缩只认最新一条：更早的快照/摘要已被其吸收
    - firstKeptEntryId 必须落在活动路径且严格位于该 compaction 之前
    - 摘要保持 user 级；compaction 之后的 system patch 续接快照
    """
    # 运行时导入避免 session 包初始化期间与 jsonl 形成循环依赖
    from mini_pi.session.models import CompactionEntry, MessageEntry

    path = list(entries)
    index = _index_entries(path)
    positions: dict[UUID, int] = {}
    latest: tuple[int, CompactionEntry] | None = None
    for position, entry in enumerate(path):
        positions[entry.id] = position
        if isinstance(entry, CompactionEntry):
            latest = (position, entry)
    if latest is None:
        return None
    compaction_at, compaction = latest

    cut_id = compaction.first_kept_entry_id
    cut_at = positions.get(cut_id)
    cut_entry = index.get(cut_id)
    if cut_at is None or cut_entry is None:
        raise SessionError(f"compaction firstKeptEntryId is not on the active path: {cut_id}")
    if not isinstance(cut_entry, MessageEntry):
        raise SessionError(
            f"compaction firstKeptEntryId must reference a message entry: {cut_id}"
        )
    if cut_at >= compaction_at:
        raise SessionError(f"compaction firstKeptEntryId must precede the compaction: {cut_id}")

    kept_messages: list[Message] = []
    trailing_system: list[SystemMessage] = []
    for position in range(cut_at, len(path)):
        entry = path[position]
        if isinstance(entry, CompactionEntry):
            continue
        if isinstance(entry.message, SystemMessage):
            # 切点与 compaction 之间的 system 已进入快照；之后的 patch 才续接
            if position > compaction_at:
                trailing_system.append(entry.message)
            continue
        kept_messages.append(entry.message)

    _validate_tool_pairs(kept_messages)
    try:
        state = replay_system_messages([compaction.system_message, *trailing_system])
    except ValueError as exc:
        raise SessionError(f"invalid system prompt history: {exc}") from exc
    if state is None:
        # compaction.system_message 必有载荷，走到这里说明模型协议被破坏
        raise SessionError("compaction entry has no system snapshot")

    summary = compaction.summary
    return CompactionProjection(
        system_prompt=state,
        summary_message=UserMessage(content=f"<{SUMMARY_TAG}>\n{summary}\n</{SUMMARY_TAG}>"),
        kept_messages=tuple(kept_messages),
    )


def _system_message_from_state(state: SystemPromptState) -> SystemMessage:
    """把回放后的 system 状态还原为唯一可折叠的 system message。"""
    if state.content is not None:
        return SystemMessage(content=state.content)
    if state.sections is None:
        raise SessionError("system prompt state has no payload")
    return SystemMessage(sections=state.sections)
