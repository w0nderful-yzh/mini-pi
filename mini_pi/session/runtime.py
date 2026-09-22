"""AgentSession 创建模式：装配 Agent 与追加式 JSONL Session。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from mini_pi.agent.agent import Agent
from mini_pi.agent.events import AgentEvent
from mini_pi.agent.state import AgentState
from mini_pi.llm.base import LLMClient
from mini_pi.llm.types import AssistantMessage, Message
from mini_pi.session.jsonl import JsonlSession
from mini_pi.tools.registry import ToolRegistry


class AgentSession:
    """为一次新会话装配 Agent，并将完整消息同步追加到 JSONL。"""

    def __init__(
        self,
        *,
        session: JsonlSession,
        llm: LLMClient,
        registry: ToolRegistry,
        max_steps: int,
        on_event: Callable[[AgentEvent], None] | None,
    ) -> None:
        self._session = session
        self._agent = Agent(
            llm=llm,
            registry=registry,
            cwd=session.header.cwd,
            max_steps=max_steps,
            on_event=on_event,
            on_message_commit=self._commit_message,
        )

    @classmethod
    def create(
        cls,
        *,
        cwd: str | Path,
        llm: LLMClient,
        registry: ToolRegistry,
        provider: str,
        model: str,
        sessions_root: str | Path | None = None,
        max_steps: int = 50,
        on_event: Callable[[AgentEvent], None] | None = None,
    ) -> AgentSession:
        """校验运行参数后创建 header，并接通 durable-first 消息提交。"""
        if max_steps <= 0:
            raise ValueError("max_steps must be > 0")
        session = JsonlSession.create(
            cwd=cwd,
            provider=provider,
            model=model,
            sessions_root=sessions_root,
        )
        return cls(
            session=session,
            llm=llm,
            registry=registry,
            max_steps=max_steps,
            on_event=on_event,
        )

    @property
    def path(self) -> Path:
        """返回当前会话的 JSONL 路径。"""
        return self._session.path

    @property
    def state(self) -> AgentState:
        """暴露当前 Agent 的内存投影供调用方检查。"""
        return self._agent.state

    def run(self, task: str) -> AssistantMessage:
        """执行一轮任务，完整消息由提交回调立即持久化。"""
        return self._agent.run(task)

    def _commit_message(self, message: Message) -> None:
        """先追加 JSONL；失败时让 Agent 的 durable-first 入口停止。"""
        # system/user 位于下一轮开始前，assistant/tool 位于 Loop 步数递增后。
        self._session.append_message(
            message,
            provider=self._session.header.provider,
            model=self._session.header.model,
            step_count=self._agent.state.step_count,
        )
