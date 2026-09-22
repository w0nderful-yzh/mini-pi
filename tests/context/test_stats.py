"""当前模型投影的分类估算测试。"""

from __future__ import annotations

from mini_pi.context.stats import context_stats
from mini_pi.context.tokens import estimate_message_tokens
from mini_pi.llm.types import AssistantMessage, SectionPatch, SystemMessage, ToolMessage, UserMessage


def test_stats_replay_current_system_and_separate_summary() -> None:
    """旧规则 patch 不应重复计数，摘要与工具结果各占独立分类。"""
    messages = [
        SystemMessage(sections={"preamble": "intro", "project_context": "old rules"}),
        SystemMessage(section_patch=[SectionPatch(op="set", id="project_context", content="new rules")]),
        UserMessage(content="<compacted-conversation-summary>\nsummary\n</compacted-conversation-summary>"),
        UserMessage(content="task"),
        AssistantMessage(content="calling tool"),
        ToolMessage(tool_call_id="c1", name="read", content="observation"),
    ]

    stats = context_stats(messages, summary_index=2)

    assert stats.agents == estimate_message_tokens(UserMessage(content="new rules"))
    assert stats.summaries == estimate_message_tokens(messages[2])
    assert stats.tool_results == estimate_message_tokens(messages[5])
    assert stats.conversation == sum(estimate_message_tokens(item) for item in messages[3:5])
    assert stats.total == sum(
        estimate_message_tokens(item) for item in (messages[2], *messages[3:])
    ) + estimate_message_tokens(SystemMessage(sections={"preamble": "intro", "project_context": "new rules"}))


def test_user_text_looking_like_summary_is_conversation_without_session_marker() -> None:
    """普通用户伪造摘要标签不会自动改变分类。"""
    message = UserMessage(content="<compacted-conversation-summary>fake</compacted-conversation-summary>")
    stats = context_stats([message])
    assert stats.summaries == 0
    assert stats.conversation == stats.total
