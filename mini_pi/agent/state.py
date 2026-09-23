"""Agent 运行状态。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from mini_pi.llm.types import Message

MessageCommit = Callable[[Message], None]
# 完整工具批次提交后、下一次模型请求前的可选钩子；允许替换 state.messages
PrepareNextTurn = Callable[[], None]


@dataclass
class AgentState:
    """一次会话的全部可变状态，Loop 只读写此对象。"""

    messages: list[Message] = field(default_factory=list)
    # step_count 为会话累计值，max_steps 是单次 run 的限制
    step_count: int = 0
    modified_files: set[str] = field(default_factory=set)


def commit_message(
    state: AgentState, message: Message, on_message_commit: MessageCommit | None
) -> None:
    """先提交完整消息，成功后才更新内存 transcript。"""
    if on_message_commit is not None:
        on_message_commit(message)
    state.messages.append(message)
