"""Agent：持有状态与依赖，负责一次任务的生命周期。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from mini_pi.agent.events import AgentEvent
from mini_pi.agent.loop import run_loop
from mini_pi.agent.prompt import build_sections
from mini_pi.agent.state import AgentState
from mini_pi.context.project import load_project_instructions
from mini_pi.context.sections import diff_sections, replay_system_messages
from mini_pi.llm.base import LLMClient
from mini_pi.llm.types import AssistantMessage, SystemMessage, UserMessage
from mini_pi.tools.registry import ToolRegistry
from mini_pi.workspace.workspace import Workspace


class Agent:
    """协调状态与项目上下文；文件读取由 Context 通过 Workspace 完成。"""

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
        self._workspace = Workspace(cwd)
        self.state = AgentState()

    def run(self, task: str) -> AssistantMessage:
        """执行一次用户任务；transcript 与步数在多次 run 之间保留。"""
        if not task.strip():
            raise ValueError("task must not be empty")
        self._refresh_system_prompt()
        self.state.messages.append(UserMessage(content=task))
        return run_loop(
            self.state,
            self._llm,
            self._registry,
            max_steps=self._max_steps,
            on_event=self._on_event,
        )

    def _refresh_system_prompt(self) -> None:
        """只在目标 sections 变化时向 transcript 追加快照或 patch。"""
        desired = {
            section.id: section.content
            for section in build_sections(
                cwd=self._workspace.root,
                tools=self._registry.schemas(),
                project_instructions=load_project_instructions(self._workspace),
            )
        }
        current = replay_system_messages(
            message
            for message in self.state.messages
            if isinstance(message, SystemMessage)
        )
        if current is None or current.sections is None:
            # 旧 content 无法安全拆分；追加完整结构化快照而非猜测差异。
            self.state.messages.append(SystemMessage(sections=desired))
            return
        patch = diff_sections(current.sections, desired)
        if patch is not None:
            self.state.messages.append(SystemMessage(section_patch=list(patch)))

    def reset(self) -> None:
        """清空会话状态，system prompt 会在下次 run 时重新注入。"""
        self.state.messages.clear()
        self.state.step_count = 0
        self.state.modified_files.clear()

    def set_llm(self, llm: LLMClient) -> None:
        """替换 LLM 客户端并保留 transcript（/connect 切换 Key 或 provider）。"""
        self._llm = llm
