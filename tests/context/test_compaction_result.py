"""M7.5d：对 plan 生成摘要与 usage 的请求、增量与失败语义测试。"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from mini_pi.context.compaction import (
    CompactionPlan,
    generate_compaction_result,
    prepare_compaction,
)
from mini_pi.errors import CompactionError, LLMError
from mini_pi.llm.types import (
    AssistantMessage,
    Message,
    SystemMessage,
    ToolCall,
    ToolMessage,
    Usage,
    UserMessage,
)
from mini_pi.session.models import CompactionEntry, MessageEntry, SessionEntry
from tests.conftest import FakeLLMClient, assistant

_TIMESTAMP = datetime(2026, 9, 23, 18, 0, tzinfo=timezone.utc)

# 字符规则每 4 字符 1 token：40 字符 = 10 token，1000 字符 = 250 token
_SMALL = 40


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
    path: list[SessionEntry], *, first_kept_entry_id: UUID, summary: str
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


def build_plan(path: list[SessionEntry], *, keep_recent_tokens: int) -> CompactionPlan:
    """准备 plan 并断言确实拿到了安全切点。"""
    plan = prepare_compaction(path, keep_recent_tokens=keep_recent_tokens).plan
    assert plan is not None
    return plan


def call_user_message(llm: FakeLLMClient) -> str:
    """取摘要请求里那条 user 消息的文本。"""
    return llm.calls[0][1].content


def test_result_serializes_plan_messages_in_single_call() -> None:
    """单次无工具调用；请求里是序列化后的待摘要消息，返回值带 usage。"""
    path: list[SessionEntry] = []
    append_message(path, SystemMessage(sections={"preamble": "p"}))
    append_message(path, UserMessage(content="a" * _SMALL))
    append_message(path, AssistantMessage(content="b" * _SMALL))
    user2 = append_message(path, UserMessage(content="c" * _SMALL))
    append_message(path, AssistantMessage(content="d" * _SMALL))
    plan = build_plan(path, keep_recent_tokens=25)
    usage = Usage(input_tokens=100, output_tokens=20, total_tokens=120)
    llm = FakeLLMClient([assistant("## Goal\n修复登陆失败").model_copy(update={"usage": usage})])

    result = generate_compaction_result(plan, llm)

    assert result.summary == "## Goal\n修复登陆失败"
    assert result.usage == usage
    assert result.plan is plan
    assert llm.tools_seen == [None]
    assert len(llm.calls) == 1
    request = call_user_message(llm)
    assert "[User]: " + "a" * _SMALL in request
    assert "[Assistant]: " + "b" * _SMALL in request
    # 保留区消息与 system 快照都不进摘要输入
    assert "c" * _SMALL not in request
    assert "preamble" not in request
    assert plan.first_kept_entry_id == user2.id


def test_repeat_compaction_updates_previous_summary_without_older_text() -> None:
    """重复压缩用 <previous-summary> 增量更新，更早的原文不再发送。"""
    path: list[SessionEntry] = []
    append_message(path, SystemMessage(sections={"preamble": "p"}))
    append_message(path, UserMessage(content="ancient-" + "a" * _SMALL))
    append_message(path, AssistantMessage(content="ancient-" + "b" * _SMALL))
    user2 = append_message(path, UserMessage(content="c" * 200))
    append_message(path, AssistantMessage(content="d" * _SMALL))
    append_compaction(path, first_kept_entry_id=user2.id, summary="old summary")
    append_message(path, UserMessage(content="e" * _SMALL))
    append_message(path, AssistantMessage(content="f" * _SMALL))
    plan = build_plan(path, keep_recent_tokens=25)
    llm = FakeLLMClient([assistant("## Goal\n继续")])

    result = generate_compaction_result(plan, llm)

    request = call_user_message(llm)
    assert result.summary == "## Goal\n继续"
    assert "<previous-summary>\nold summary\n</previous-summary>" in request
    assert "NEW conversation messages" in request
    assert "[User]: " + "c" * 200 in request
    # 上一次已摘要的原文与 system 快照都不再发送
    assert "ancient-" not in request
    assert "preamble" not in request


def test_split_turn_keeps_pending_tool_round_out_of_summary() -> None:
    """切点落在 assistant 上时工具轮整体保留，摘要输入保留该轮请求作为上下文。"""
    path: list[SessionEntry] = []
    append_message(path, SystemMessage(sections={"preamble": "p"}))
    user1 = append_message(path, UserMessage(content="a" * 1000))
    call = append_message(
        path,
        AssistantMessage(
            tool_calls=[ToolCall(id="call_1", name="read", arguments={"path": "f.txt"})]
        ),
    )
    result_entry = append_message(
        path,
        ToolMessage(tool_call_id="call_1", name="read", content="r" * 400),
    )
    append_message(path, UserMessage(content="c" * _SMALL))
    append_message(path, AssistantMessage(content="d" * _SMALL))
    plan = build_plan(path, keep_recent_tokens=200)
    llm = FakeLLMClient([assistant("## Goal\n继续读代码")])

    generate_compaction_result(plan, llm)

    assert plan.cut.boundary == "assistant"
    # 未完成的工具轮整体留在保留区：调用与其结果都不进摘要输入
    assert call.id in plan.kept_entry_ids
    assert result_entry.id in plan.kept_entry_ids
    request = call_user_message(llm)
    assert "[User]: " + "a" * 1000 in request
    assert "call_1" not in request


def test_failure_leaves_plan_and_messages_untouched() -> None:
    """摘要失败时不产生结果，plan 与其消息保持原样。"""
    path: list[SessionEntry] = []
    append_message(path, SystemMessage(sections={"preamble": "p"}))
    append_message(path, UserMessage(content="a" * _SMALL))
    append_message(path, AssistantMessage(content="b" * _SMALL))
    append_message(path, UserMessage(content="c" * _SMALL))
    append_message(path, AssistantMessage(content="d" * _SMALL))
    plan = build_plan(path, keep_recent_tokens=25)
    before = plan.messages_to_summarize
    llm = FakeLLMClient([LLMError("connection reset", retryable=True)])

    with pytest.raises(LLMError, match="connection reset"):
        generate_compaction_result(plan, llm)

    assert plan.messages_to_summarize == before
    assert plan.kept_entry_ids == tuple(entry.id for entry in path)[3:]


def test_all_empty_messages_fail_before_request() -> None:
    """消息正文全为空时没有可摘要的事实，不发请求也不返回半成品。"""
    path: list[SessionEntry] = []
    append_message(path, SystemMessage(sections={"preamble": "p"}))
    # 待摘要的 user 消息只有空白：token 估算非零，但序列化后没有任何事实
    append_message(path, UserMessage(content=" " * _SMALL))
    append_message(path, AssistantMessage(content="x" * _SMALL))
    plan = build_plan(path, keep_recent_tokens=5)
    llm = FakeLLMClient([])

    with pytest.raises(CompactionError, match="no serializable transcript"):
        generate_compaction_result(plan, llm)

    assert llm.calls == []
