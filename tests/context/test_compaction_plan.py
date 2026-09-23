"""M7.5c：压缩输入 plan 的切点映射、快照与无切点原因测试。"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from mini_pi.context.compaction import prepare_compaction
from mini_pi.context.tokens import TokenEstimate
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

_TIMESTAMP = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)

# 字符规则每 4 字符 1 token：40 字符 = 10 token，200 字符 = 50 token
_SMALL = 40
_LARGE = 200


def append_message(
    path: list[SessionEntry], message: Message, *, entry_id: UUID | None = None
) -> MessageEntry:
    """在链尾追加 message entry，返回新建 entry 以便断言 id。"""
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
    path: list[SessionEntry], *, first_kept_entry_id: UUID, summary: str = "old summary"
) -> CompactionEntry:
    """在链尾追加 compaction entry，模拟上一次已完成的压缩。"""
    entry = CompactionEntry(
        type="compaction",
        id=uuid4(),
        parent_id=path[-1].id,
        timestamp=_TIMESTAMP,
        summary=summary,
        first_kept_entry_id=first_kept_entry_id,
        tokens_before=1234,
        system_message=SystemMessage(sections={"preamble": "p"}),
    )
    path.append(entry)
    return entry


def tool_round(path: list[SessionEntry]) -> tuple[MessageEntry, MessageEntry]:
    """追加一次完整的 read 工具轮（调用 + 结果），返回两个 entry。"""
    call = append_message(
        path,
        AssistantMessage(tool_calls=[ToolCall(id="call_1", name="read", arguments={"path": "f.txt"})]),
    )
    result = append_message(
        path,
        ToolMessage(
            tool_call_id="call_1",
            name="read",
            content="r" * 400,
            modified_files=["b.py"],
        ),
    )
    return call, result


def test_plan_maps_cut_to_real_entries_and_keeps_path_partition() -> None:
    """切点映射回真实 entry；待摘要与保留 id 恰好分割活动路径。"""
    path: list[SessionEntry] = []
    system = append_message(path, SystemMessage(sections={"preamble": "p"}))
    user1 = append_message(path, UserMessage(content="a" * _SMALL))
    assistant1 = append_message(path, AssistantMessage(content="b" * _SMALL))
    user2 = append_message(path, UserMessage(content="c" * _SMALL))
    assistant2 = append_message(path, AssistantMessage(content="d" * _SMALL))

    preparation = prepare_compaction(path, keep_recent_tokens=25)

    plan = preparation.plan
    assert plan is not None
    assert plan.cut.boundary == "user"
    assert plan.first_kept_entry_id == user2.id
    assert plan.kept_entry_ids == (user2.id, assistant2.id)
    # system entry 也被吸收（内容由 system 快照承载），但不进入摘要输入
    assert plan.summarized_entry_ids == (system.id, user1.id, assistant1.id)
    assert plan.messages_to_summarize == (user1.message, assistant1.message)
    assert set(plan.summarized_entry_ids) | set(plan.kept_entry_ids) == {
        entry.id for entry in path
    }
    assert plan.previous_summary is None
    # 1 + 10 + 10 + 10 + 10 token：system 快照加四条消息
    assert plan.tokens_before == TokenEstimate(tokens=41, source="estimated")
    assert plan.system_message == SystemMessage(sections={"preamble": "p"})
    assert plan.modified_files == ()
    assert preparation.reason == "ready: summarize 2 messages, keep 2 entries"


def test_no_cut_when_recent_messages_fit_budget() -> None:
    """预算内无需压缩时不给 plan，并说明原因。"""
    path: list[SessionEntry] = []
    append_message(path, SystemMessage(sections={"preamble": "p"}))
    append_message(path, UserMessage(content="a" * _SMALL))

    preparation = prepare_compaction(path, keep_recent_tokens=1000)

    assert preparation.plan is None
    assert preparation.reason == "no safe cut point: recent messages fit within keep_recent_tokens"
    assert prepare_compaction([], keep_recent_tokens=100).plan is None


def test_repeat_compaction_reuses_previous_summary_without_resending_it() -> None:
    """重复压缩把旧摘要交给 previous_summary，只序列化新消息。"""
    path: list[SessionEntry] = []
    append_message(path, SystemMessage(sections={"preamble": "p"}))
    append_message(path, UserMessage(content="a" * _SMALL))
    append_message(path, AssistantMessage(content="b" * _SMALL))
    user2 = append_message(path, UserMessage(content="c" * _LARGE))
    assistant2 = append_message(path, AssistantMessage(content="d" * _SMALL))
    append_compaction(path, first_kept_entry_id=user2.id)
    user3 = append_message(path, UserMessage(content="e" * _SMALL))
    assistant3 = append_message(path, AssistantMessage(content="f" * _SMALL))

    preparation = prepare_compaction(path, keep_recent_tokens=25)

    plan = preparation.plan
    assert plan is not None
    assert plan.previous_summary == "old summary"
    assert plan.first_kept_entry_id == user3.id
    assert plan.kept_entry_ids == (user3.id, assistant3.id)
    # 上次保留区间内的消息现在可以进入摘要，但旧摘要本身只走 previous_summary
    assert plan.summarized_entry_ids == (user2.id, assistant2.id)
    assert plan.messages_to_summarize == (user2.message, assistant2.message)
    assert "old summary" not in "".join(
        message.content for message in plan.messages_to_summarize
    )


def test_cut_never_splits_tool_round_and_collects_modified_files() -> None:
    """工具调用与其结果始终同侧；待摘要范围内的改动文件进入 plan。"""
    path: list[SessionEntry] = []
    system = append_message(path, SystemMessage(sections={"preamble": "p"}))
    user1 = append_message(path, UserMessage(content="a" * _SMALL))
    call, result = tool_round(path)
    user2 = append_message(path, UserMessage(content="c" * _LARGE))
    assistant2 = append_message(path, AssistantMessage(content="d" * _SMALL))

    preparation = prepare_compaction(path, keep_recent_tokens=60)

    plan = preparation.plan
    assert plan is not None
    # 工具调用与结果同段：要么一起被摘要，要么一起保留，不会被切开
    assert plan.summarized_entry_ids == (system.id, user1.id, call.id, result.id)
    assert plan.messages_to_summarize == (user1.message, call.message, result.message)
    assert plan.kept_entry_ids == (user2.id, assistant2.id)
    assert plan.modified_files == ("b.py",)


def test_cut_on_synthetic_messages_reports_no_new_content() -> None:
    """切点只够到 system 快照与旧摘要时，没有新内容可摘要。"""
    path: list[SessionEntry] = []
    append_message(path, SystemMessage(sections={"preamble": "p"}))
    append_message(path, UserMessage(content="a" * _SMALL))
    append_message(path, AssistantMessage(content="b" * _SMALL))
    user2 = append_message(path, UserMessage(content="c" * _LARGE))
    append_message(path, AssistantMessage(content="d" * _SMALL))
    append_compaction(path, first_kept_entry_id=user2.id)
    append_message(path, UserMessage(content="e" * _SMALL))
    append_message(path, AssistantMessage(content="f" * _SMALL))

    preparation = prepare_compaction(path, keep_recent_tokens=90)

    assert preparation.plan is None
    assert preparation.reason == "cut point leaves no new messages to summarize"


def test_missing_system_snapshot_is_a_protocol_error() -> None:
    """没有 system 历史时写不出 CompactionEntry 快照，必须明确失败。"""
    path: list[SessionEntry] = []
    append_message(path, UserMessage(content="a" * _SMALL))
    append_message(path, AssistantMessage(content="b" * _SMALL))

    with pytest.raises(SessionError, match="requires a system prompt snapshot"):
        prepare_compaction(path, keep_recent_tokens=15)
