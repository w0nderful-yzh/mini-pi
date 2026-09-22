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


def _format_arguments(arguments: dict[str, object], *, limit: int = 120) -> str:
    """把参数压成单行 JSON；过长截断，避免一行刷屏。"""
    rendered = json.dumps(arguments, ensure_ascii=False)
    if len(rendered) > limit:
        return rendered[: limit - 1] + "…"
    return rendered


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
            usage = event.message.usage
            if usage is not None and usage.total_tokens > 0:
                # provider 精确用量；无 usage 时保持安静，不伪装估算
                self.console.print(
                    f"  tokens: in {usage.input_tokens} / out {usage.output_tokens}",
                    style="dim",
                    markup=False,
                )
        elif isinstance(event, ToolExecutionStartEvent):
            arguments = _format_arguments(event.tool_call.arguments)
            self.console.print(
                f"→ {event.tool_call.name} {arguments}",
                style="cyan",
                markup=False,
                highlight=False,
            )
        elif isinstance(event, ToolExecutionEndEvent):
            line = f"  {_preview(event.result.content)}"
            if event.result.modified_files:
                line += f" · {len(event.result.modified_files)} file(s) changed"
            style = "red" if event.is_error else "green"
            self.console.print(line, style=style, markup=False, highlight=False)
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
