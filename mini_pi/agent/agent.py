"""Agent：持有状态与依赖，负责一次任务的生命周期。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from mini_pi.agent.events import AgentEvent
from mini_pi.agent.loop import run_loop
from mini_pi.agent.prompt import build_system_prompt
from mini_pi.agent.state import AgentState
from mini_pi.llm.base import LLMClient
from mini_pi.llm.types import AssistantMessage, SystemMessage, UserMessage
from mini_pi.tools.registry import ToolRegistry


class Agent:
    """只持有 cwd 与工具注册表；文件与 Shell 能力全部来自 Tool 层。"""

    def __init__(
        self,
        *,
        llm: LLMClient,
        registry: ToolRegistry,
        cwd: Path,
        max_steps: int = 50,
        on_event: Callable[[AgentEvent], None] | None = None,
    ) -> None:
        if max_steps <= 0:
            raise ValueError("max_steps must be > 0")
        self._llm = llm
        self._registry = registry
        self._max_steps = max_steps
        self._on_event = on_event
        self._system_prompt = build_system_prompt(cwd=cwd, tools=registry.schemas())
        self.state = AgentState()

    def run(self, task: str) -> AssistantMessage:
        """执行一次用户任务；transcript 与步数在多次 run 之间保留。"""
        if not task.strip():
            raise ValueError("task must not be empty")
        if not self.state.messages:
            self.state.messages.append(SystemMessage(content=self._system_prompt))
        self.state.messages.append(UserMessage(content=task))
        return run_loop(
            self.state,
            self._llm,
            self._registry,
            max_steps=self._max_steps,
            on_event=self._on_event,
        )

    def reset(self) -> None:
        """清空会话状态，system prompt 会在下次 run 时重新注入。"""
        self.state.messages.clear()
        self.state.step_count = 0
        self.state.modified_files.clear()
