"""当前模型投影的分类 token 估算。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from mini_pi.context.sections import replay_system_messages
from mini_pi.context.tokens import estimate_message_tokens
from mini_pi.llm.types import Message, SystemMessage, ToolMessage, UserMessage


@dataclass(frozen=True, slots=True)
class ContextStats:
    """各类估算之和是 total；Provider usage 应另行展示。"""

    system: int
    agents: int
    conversation: int
    tool_results: int
    summaries: int

    @property
    def total(self) -> int:
        """返回当前投影的分类估算总和。"""
        return self.system + self.agents + self.conversation + self.tool_results + self.summaries


def context_stats(messages: Sequence[Message], *, summary_index: int | None = None) -> ContextStats:
    """按当前投影计算分类估算，旧 system patch 只回放一次。"""
    system_messages = [item for item in messages if isinstance(item, SystemMessage)]
    state = replay_system_messages(system_messages)
    system = agents = conversation = tool_results = summaries = 0
    if state is not None:
        if state.content is not None:
            system = estimate_message_tokens(SystemMessage(content=state.content))
        elif state.sections is not None:
            # 项目规则独立列出；只统计最新完整快照，不累计历史 patch。
            agents_text = state.sections.get("project_context", "")
            agents = estimate_message_tokens(UserMessage(content=agents_text))
            system = estimate_message_tokens(SystemMessage(sections=state.sections)) - agents
    for index, message in enumerate(messages):
        if isinstance(message, SystemMessage):
            continue
        tokens = estimate_message_tokens(message)
        if index == summary_index:
            summaries += tokens
        elif isinstance(message, ToolMessage):
            tool_results += tokens
        else:
            conversation += tokens
    return ContextStats(system, agents, conversation, tool_results, summaries)
