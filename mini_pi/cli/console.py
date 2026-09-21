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


def _preview(content: str, *, limit: int = 200) -> str:
    """取首个非空行做预览；全空白内容返回 (empty)，超长行加省略号。"""
    for line in content.splitlines():
        if line.strip():
            return line[:limit] + ("…" if len(line) > limit else "")
    return "(empty)"


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
            self.console.print(f"  {_preview(event.result.content)}", style=style, markup=False)
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
