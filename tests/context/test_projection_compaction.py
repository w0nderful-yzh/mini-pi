"""M7.4c：compaction entry 到上下文投影的测试。"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from mini_pi.context.projection import (
    SUMMARY_TAG,
    project_compaction,
    project_entry_path,
)
from mini_pi.errors import SessionError
from mini_pi.llm.types import (
    AssistantMessage,
    Message,
    SystemMessage,
    ToolCall,
    ToolMessage,
    UserMessage,
)
from mini_pi.session.models import CompactionEntry, MessageEntry, SessionEntry

_TIMESTAMP = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)


def append_message(
    path: list[SessionEntry], message: Message, *, entry_id: UUID | None = None
) -> MessageEntry:
    """在链尾追加 message entry，返回新建 entry。"""
    entry = MessageEntry(
        type="message",
        id=entry_id or uuid4(),
        parent_id=path[-1].id if path else None,
        timestamp=_TIMESTAMP,
        provider="deepseek",
        model="deepseek-chat",
        message=message,
    )
    path.append(entry)
    return entry


def append_compaction(
    path: list[SessionEntry],
    *,
    first_kept_entry_id: UUID,
    summary: str = "goal: finish the task",
    system_message: SystemMessage | None = None,
) -> CompactionEntry:
    """在链尾追加 compaction entry，默认带一个结构化 system 快照。"""
    entry = CompactionEntry(
        type="compaction",
        id=uuid4(),
        parent_id=path[-1].id if path else None,
        timestamp=_TIMESTAMP,
        summary=summary,
        first_kept_entry_id=first_kept_entry_id,
        tokens_before=1234,
        system_message=system_message or SystemMessage(sections={"preamble": "p"}),
    )
    path.append(entry)
    return entry


def tool_call(call_id: str) -> ToolCall:
    return ToolCall(id=call_id, name="read", arguments={"path": "f.txt"})


def test_no_compaction_returns_none() -> None:
    """没有 compaction 时由 message 投影负责，本函数返回 None。"""
    path: list[SessionEntry] = []
    append_message(path, UserMessage(content="task"))
    append_message(path, AssistantMessage(content="done"))

    assert project_compaction(path) is None


def test_single_compaction_projects_snapshot_summary_and_kept_messages() -> None:
    """一次压缩：system 快照 + 摘要 + 切点起保留消息 + 压缩后新消息。"""
    path: list[SessionEntry] = []
    append_message(path, UserMessage(content="old task"))
    append_message(path, AssistantMessage(content="old reply"))
    kept_user = append_message(path, UserMessage(content="kept task"))
    kept_tool_round = append_message(path, AssistantMessage(tool_calls=[tool_call("c1")]))
    append_message(path, ToolMessage(tool_call_id="c1", name="read", content="data"))
    append_compaction(path, first_kept_entry_id=kept_user.id)
    after = append_message(path, AssistantMessage(content="after compaction"))

    projection = project_compaction(path)

    assert projection is not None
    assert projection.system_prompt.sections == {"preamble": "p"}
    assert projection.summary_message.role == "user"
    assert f"<{SUMMARY_TAG}>" in projection.summary_message.content
    assert "goal: finish the task" in projection.summary_message.content
    assert projection.kept_messages == (
        kept_user.message,
        kept_tool_round.message,
        path[4].message,
        after.message,
    )
    # messages 顺序固定为 system 快照 → 摘要 → 保留消息
    assert isinstance(projection.messages[0], SystemMessage)
    assert projection.messages[1] is projection.summary_message
    assert projection.messages[2:] == projection.kept_messages


def test_repeated_compaction_uses_latest_projection_only() -> None:
    """连续两次压缩只取最新一次的快照、摘要与切点。"""
    path: list[SessionEntry] = []
    first_kept = append_message(path, UserMessage(content="first kept"))
    append_message(path, AssistantMessage(content="first reply"))
    first_comp = append_compaction(
        path,
        first_kept_entry_id=first_kept.id,
        summary="first summary",
        system_message=SystemMessage(sections={"preamble": "first"}),
    )
    assert first_comp.summary == "first summary"
    second_kept = append_message(path, UserMessage(content="second kept"))
    append_compaction(
        path,
        first_kept_entry_id=second_kept.id,
        summary="second summary",
        system_message=SystemMessage(sections={"preamble": "second"}),
    )
    tail = append_message(path, AssistantMessage(content="tail"))

    projection = project_compaction(path)

    assert projection is not None
    assert projection.system_prompt.sections == {"preamble": "second"}
    assert "second summary" in projection.summary_message.content
    # 第一次压缩的 entry 与旧摘要不会再出现
    assert projection.kept_messages == (
        second_kept.message,
        tail.message,
    )


def test_system_patch_after_compaction_applies_to_snapshot() -> None:
    """压缩之后的 system patch 续接在快照上，而不是提升摘要权限。"""
    path: list[SessionEntry] = []
    kept = append_message(path, UserMessage(content="kept"))
    append_compaction(path, first_kept_entry_id=kept.id)
    append_message(
        path,
        SystemMessage(section_patch=[{"op": "set", "id": "preamble", "content": "patched"}]),
    )

    projection = project_compaction(path)

    assert projection is not None
    assert projection.system_prompt.sections == {"preamble": "patched"}
    assert [message.role for message in projection.messages] == ["system", "user", "user"]
    assert projection.messages[1] is projection.summary_message


def test_old_branch_compaction_is_ignored() -> None:
    """兄弟分支上的 compaction 不属于活动路径，不参与投影。"""
    root = MessageEntry(
        type="message",
        id=uuid4(),
        parent_id=None,
        timestamp=_TIMESTAMP,
        provider="deepseek",
        model="deepseek-chat",
        message=UserMessage(content="root"),
    )
    left = MessageEntry(
        type="message",
        id=uuid4(),
        parent_id=root.id,
        timestamp=_TIMESTAMP,
        provider="deepseek",
        model="deepseek-chat",
        message=UserMessage(content="left"),
    )
    sibling = MessageEntry(
        type="message",
        id=uuid4(),
        parent_id=root.id,
        timestamp=_TIMESTAMP,
        provider="deepseek",
        model="deepseek-chat",
        message=UserMessage(content="sibling"),
    )
    branch_compaction = CompactionEntry(
        type="compaction",
        id=uuid4(),
        parent_id=sibling.id,
        timestamp=_TIMESTAMP,
        summary="branch summary",
        first_kept_entry_id=sibling.id,
        tokens_before=10,
        system_message=SystemMessage(content="branch system"),
    )
    entries: list[SessionEntry] = [root, left, sibling, branch_compaction]

    active_path = project_entry_path(entries, leaf_id=left.id)

    assert [entry.id for entry in active_path] == [root.id, left.id]
    assert project_compaction(active_path) is None


def test_rejects_cut_point_not_on_active_path() -> None:
    """切点不在活动路径上时必须报错，而不是退回全量投影。"""
    path: list[SessionEntry] = []
    append_message(path, UserMessage(content="kept"))
    append_compaction(path, first_kept_entry_id=uuid4())

    with pytest.raises(SessionError, match="active path"):
        project_compaction(path)


def test_rejects_cut_point_after_compaction() -> None:
    """切点必须位于 compaction 之前。"""
    path: list[SessionEntry] = []
    append_message(path, UserMessage(content="old"))
    later_id = uuid4()
    append_compaction(path, first_kept_entry_id=later_id)
    append_message(path, UserMessage(content="later"), entry_id=later_id)

    with pytest.raises(SessionError, match="must precede"):
        project_compaction(path)


def test_rejects_non_message_cut_point() -> None:
    """切点必须指向 message entry。"""
    path: list[SessionEntry] = []
    append_message(path, UserMessage(content="kept"))
    first = append_compaction(path, first_kept_entry_id=path[0].id)
    append_compaction(path, first_kept_entry_id=first.id)

    with pytest.raises(SessionError, match="must reference a message entry"):
        project_compaction(path)


def test_rejects_broken_tool_batch_in_kept_range() -> None:
    """保留区间若从中途工具轮开始，配对校验必须失败。"""
    path: list[SessionEntry] = []
    append_message(path, UserMessage(content="old"))
    orphan_cut = append_message(
        path, AssistantMessage(tool_calls=[tool_call("c1")])
    )
    append_compaction(path, first_kept_entry_id=orphan_cut.id)

    with pytest.raises(SessionError, match="missing tool result"):
        project_compaction(path)
