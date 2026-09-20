"""Agent 运行状态。"""

from __future__ import annotations

from dataclasses import dataclass, field

from mini_pi.llm.types import Message


@dataclass
class AgentState:
    """一次会话的全部可变状态，Loop 只读写此对象。"""

    messages: list[Message] = field(default_factory=list)
    # step_count 为会话累计值，max_steps 是单次 run 的限制
    step_count: int = 0
    modified_files: set[str] = field(default_factory=set)
