"""CLI 渲染器：AgentEvent -> Rich 终端输出。"""

from __future__ import annotations

import json
from importlib import resources

from rich.console import Console
from rich.live import Live

from mini_pi.agent.events import (
    AgentEndEvent,
    AgentEvent,
    AgentStartEvent,
    MessageDeltaEvent,
    MessageEndEvent,
    MessageStartEvent,
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

    def __init__(self, console: Console | None = None, *, show_thinking: bool = True) -> None:
        self.console = console or Console()
        self._printing_text = False
        self._show_thinking = show_thinking
        self._thinking: Live | None = None
        self._input_tokens = 0
        self._output_tokens = 0
        self._has_usage = False

    def _start_thinking(self) -> None:
        """只在足够宽的交互终端显示瞬时状态，不把图案写进日志。"""
        if not self._show_thinking or not self.console.is_terminal:
            return
        art = resources.files("mini_pi").joinpath("assets/thinking.txt").read_text(encoding="utf-8")
        if self.console.width < max(len(line) for line in art.splitlines()):
            return
        self._thinking = Live(
            art.rstrip("\n"), console=self.console, auto_refresh=False, transient=True
        )
        self._thinking.start()

    def _stop_thinking(self) -> None:
        """在正文或工具输出前清理状态，避免残留和重复刷屏。"""
        if self._thinking is not None:
            self._thinking.stop()
            self._thinking = None

    def close(self) -> None:
        """运行中断或抛错时清理终端状态。"""
        self._stop_thinking()

    def handle(self, event: AgentEvent) -> None:
        if isinstance(event, AgentStartEvent):
            self._input_tokens = 0
            self._output_tokens = 0
            self._has_usage = False
        elif isinstance(event, MessageStartEvent):
            self._start_thinking()
        elif isinstance(event, MessageDeltaEvent):
            if event.kind == "thinking":
                return
            self._stop_thinking()
            self.console.print(event.delta, end="", markup=False, highlight=False)
            self._printing_text = True
        elif isinstance(event, MessageEndEvent):
            self._stop_thinking()
            if self._printing_text:
                self.console.print()
                self._printing_text = False
            usage = event.message.usage
            if usage is not None and usage.total_tokens > 0:
                # 只累计 Provider 返回的真实请求用量；最终统一放在任务摘要。
                self._input_tokens += usage.input_tokens
                self._output_tokens += usage.output_tokens
                self._has_usage = True
        elif isinstance(event, ToolExecutionStartEvent):
            self._stop_thinking()
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
            self._stop_thinking()
            self._render_end(event)
            if self._has_usage:
                self.console.print(
                    f"  provider tokens: in {self._input_tokens} / out {self._output_tokens}",
                    style="dim",
                    markup=False,
                )

    def _render_end(self, event: AgentEndEvent) -> None:
        if event.reason == "step_limit":
            self.console.print(
                "Reached the step limit before finishing the task.", style="yellow", markup=False
            )
        elif event.reason == "error":
            self.console.print(
                f"Agent stopped with an error: {event.error}", style="red", markup=False
            )
