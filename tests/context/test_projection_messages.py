"""M7.4b：message entry path 到模型 messages 的投影测试。"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from mini_pi.context.projection import project_messages
from mini_pi.context.sections import SystemPromptState
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


def tool_call(call_id: str) -> ToolCall:
    return ToolCall(id=call_id, name="read", arguments={"path": "f.txt"})


def build_path(messages: list[Message]) -> list[SessionEntry]:
    """把消息顺序串成一条 parent 链，专注投影而非树形状。"""
    path: list[SessionEntry] = []
    parent: UUID | None = None
    for message in messages:
        entry_id = uuid4()
        path.append(
            MessageEntry(
                type="message",
                id=entry_id,
                parent_id=parent,
                timestamp=_TIMESTAMP,
                provider="deepseek",
                model="deepseek-chat",
                message=message,
            )
        )
        parent = entry_id
    return path


def test_projects_four_roles_in_order() -> None:
    """四种 role 按 entry 顺序还原，tool 结果紧跟其 assistant。"""
    path = build_path(
        [
            SystemMessage(sections={"preamble": "p", "rules": "r"}),
            UserMessage(content="task"),
            AssistantMessage(tool_calls=[tool_call("c1")]),
            ToolMessage(tool_call_id="c1", name="read", content="data"),
            AssistantMessage(content="done"),
        ]
    )

    projection = project_messages(path)

    assert [message.role for message in projection.messages] == [
        "system",
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert projection.system_prompt == SystemPromptState(
        sections={"preamble": "p", "rules": "r"}
    )


def test_restores_section_patch_on_top_of_snapshot() -> None:
    """结构化 section 状态要把后续 patch 应用到快照上。"""
    path = build_path(
        [
            SystemMessage(sections={"preamble": "p", "rules": "old"}),
            SystemMessage(
                section_patch=[{"op": "set", "id": "rules", "content": "new"}]
            ),
            UserMessage(content="task"),
        ]
    )

    projection = project_messages(path)

    assert projection.system_prompt is not None
    assert projection.system_prompt.sections == {"preamble": "p", "rules": "new"}


def test_legacy_system_content_is_opaque() -> None:
    """M7.1 时代的 content system message 作为完整快照回放。"""
    projection = project_messages(build_path([SystemMessage(content="legacy prompt")]))

    assert projection.system_prompt == SystemPromptState(content="legacy prompt")
    assert projection.messages == (SystemMessage(content="legacy prompt"),)


def test_no_system_message_yields_none_state() -> None:
    """没有 system message 时结构化状态为空。"""
    projection = project_messages(build_path([UserMessage(content="hi")]))

    assert projection.system_prompt is None
    assert [message.role for message in projection.messages] == ["user"]


def test_consecutive_tool_rounds_pair_correctly() -> None:
    """连续工具轮：每个 assistant 的多工具批次都应完整配对。"""
    path = build_path(
        [
            UserMessage(content="task"),
            AssistantMessage(tool_calls=[tool_call("c1"), tool_call("c2")]),
            ToolMessage(tool_call_id="c2", name="read", content="second"),
            ToolMessage(tool_call_id="c1", name="read", content="first"),
            AssistantMessage(tool_calls=[tool_call("c3")]),
            ToolMessage(tool_call_id="c3", name="read", content="third"),
            AssistantMessage(content="done"),
        ]
    )

    projection = project_messages(path)

    assert [message.role for message in projection.messages] == [
        "user",
        "assistant",
        "tool",
        "tool",
        "assistant",
        "tool",
        "assistant",
    ]


def test_rejects_missing_tool_result() -> None:
    """assistant 请求工具却没有对应结果必须失败。"""
    path = build_path(
        [
            UserMessage(content="task"),
            AssistantMessage(tool_calls=[tool_call("c1")]),
            UserMessage(content="next"),
        ]
    )

    with pytest.raises(SessionError, match="missing tool result"):
        project_messages(path)


def test_rejects_partial_multi_tool_batch() -> None:
    """多工具批次只回了一个结果时不得当作完整。"""
    path = build_path(
        [
            AssistantMessage(tool_calls=[tool_call("c1"), tool_call("c2")]),
            ToolMessage(tool_call_id="c1", name="read", content="first"),
            AssistantMessage(content="done"),
        ]
    )

    with pytest.raises(SessionError, match="missing tool result"):
        project_messages(path)


def test_rejects_orphan_tool_result() -> None:
    """没有对应 tool call 的结果是孤儿，不能静默丢弃。"""
    path = build_path(
        [ToolMessage(tool_call_id="c1", name="read", content="data")]
    )

    with pytest.raises(SessionError, match="without matching tool call"):
        project_messages(path)


def test_rejects_duplicate_tool_call_id() -> None:
    """同一 call id 出现两次会让配对有歧义。"""
    path = build_path(
        [
            AssistantMessage(tool_calls=[tool_call("c1")]),
            ToolMessage(tool_call_id="c1", name="read", content="first"),
            AssistantMessage(tool_calls=[tool_call("c1")]),
            ToolMessage(tool_call_id="c1", name="read", content="second"),
        ]
    )

    with pytest.raises(SessionError, match="duplicate tool_call id"):
        project_messages(path)


def test_rejects_duplicate_tool_result() -> None:
    """同一个 tool call 收到两个结果必须失败。"""
    path = build_path(
        [
            AssistantMessage(tool_calls=[tool_call("c1")]),
            ToolMessage(tool_call_id="c1", name="read", content="first"),
            ToolMessage(tool_call_id="c1", name="read", content="second"),
        ]
    )

    with pytest.raises(SessionError, match="without matching tool call"):
        project_messages(path)


def test_rejects_compaction_entry() -> None:
    """本任务不解释 compaction，遇到即提示交给 M7.4c。"""
    path = build_path([UserMessage(content="task")])
    path.append(
        CompactionEntry(
            type="compaction",
            id=uuid4(),
            parent_id=path[-1].id,
            timestamp=_TIMESTAMP,
            summary="summary",
            first_kept_entry_id=path[-1].id,
            tokens_before=10,
            system_message=SystemMessage(content="system"),
        )
    )

    with pytest.raises(SessionError, match="M7.4c"):
        project_messages(path)
