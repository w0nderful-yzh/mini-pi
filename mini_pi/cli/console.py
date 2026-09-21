"""CLI 渲染器：AgentEvent -> Rich 终端输出。"""

from __future__ import annotations

import json

from rich.console import Console

from mini_pi.agent.events import (
    AgentEndEvent,
    AgentEvent,
    MessageDeltaEvent,
    MessageEndEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
)


class ConsoleRenderer:
    """on_event 消费者：只做渲染，不参与任何决策。"""

    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()
        self._printing_text = False

    def handle(self, event: AgentEvent) -> None:
        if isinstance(event, MessageDeltaEvent):
            # thinking 用暗色区分；逐段打印不换行，message_end 时统一收尾
            style = "dim" if event.kind == "thinking" else None
            self.console.print(event.delta, end="", style=style, markup=False, highlight=False)
            self._printing_text = True
        elif isinstance(event, MessageEndEvent):
            if self._printing_text:
                self.console.print()
                self._printing_text = False
        elif isinstance(event, ToolExecutionStartEvent):
            arguments = json.dumps(event.tool_call.arguments, ensure_ascii=False)
            self.console.print(f"→ {event.tool_call.name} {arguments}", style="cyan", markup=False)
        elif isinstance(event, ToolExecutionEndEvent):
            style = "red" if event.is_error else "green"
            preview = event.result.content.splitlines()[0] if event.result.content else "(empty)"
            self.console.print(f"  {preview[:200]}", style=style, markup=False)
        elif isinstance(event, AgentEndEvent):
            self._render_end(event)

    def _render_end(self, event: AgentEndEvent) -> None:
        if event.reason == "step_limit":
            self.console.print(
                "Reached the step limit before finishing the task.", style="yellow", markup=False
            )
        elif event.reason == "error":
            self.console.print(
                f"Agent stopped with an error: {event.error}", style="red", markup=False
            )
