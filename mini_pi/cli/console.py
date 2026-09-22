"""CLI 渲染器：AgentEvent -> Rich 终端输出。"""

from __future__ import annotations

import json
import re
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


def _format_arguments(arguments: dict[str, object], *, limit: int | None = 120) -> str:
    """把参数压成单行 JSON；过长截断，避免一行刷屏。"""
    rendered = json.dumps(arguments, ensure_ascii=False)
    if limit is not None and len(rendered) > limit:
        return rendered[: limit - 1] + "…"
    return rendered


def _tool_action(name: str, arguments: dict[str, object]) -> str:
    """默认展示工具意图，不输出整段参数或文件内容。"""
    path = arguments.get("path")
    if name in {"read", "write", "edit"} and isinstance(path, str):
        return f"{name.capitalize()} {path}"
    if name == "search":
        return "Search workspace"
    if name == "bash":
        return "Run shell command"
    if name == "git_diff":
        return "Inspect git diff"
    return name


def _tool_result(event: ToolExecutionEndEvent) -> str:
    """摘要保留退出码、超时、截断和改动数量等关键结果。"""
    details = event.result.details or {}
    if event.is_error:
        line = f"failed: {_preview(event.result.content)}"
    elif event.tool_call.name == "bash":
        code = details.get("exit_code")
        line = f"shell exited {code}" if isinstance(code, int) else _preview(event.result.content)
        if details.get("timed_out"):
            line += " (timed out)"
        if details.get("stdout_truncated") or details.get("stderr_truncated"):
            line += " (output truncated)"
    elif event.tool_call.name == "search" and isinstance(details.get("count"), int):
        line = f"{details['count']} matches"
    elif event.tool_call.name in {"read", "write", "edit", "git_diff"}:
        line = "completed"
    else:
        line = _preview(event.result.content)
    if event.result.modified_files:
        line += f" · {len(event.result.modified_files)} file(s) changed"
    return line


class ConsoleRenderer:
    """on_event 消费者：只做渲染，不参与任何决策。"""

    def __init__(
        self,
        console: Console | None = None,
        *,
        show_thinking: bool = True,
        verbose: bool = False,
    ) -> None:
        self.console = console or Console()
        self._printing_text = False
        self._show_thinking = show_thinking
        self._verbose = verbose
        self._secrets: tuple[str, ...] = ()
        self._thinking: Live | None = None
        self._input_tokens = 0
        self._output_tokens = 0
        self._has_usage = False
        self.last_tool_count = 0

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

    def set_secrets(self, values: list[str]) -> None:
        """登记已配置凭据，避免 verbose 输出中直接出现其值。"""
        self._secrets = tuple(value for value in values if value)

    def _redact(self, value: str) -> str:
        """屏蔽已知凭据与常见 API Key 赋值形式。"""
        for secret in self._secrets:
            value = value.replace(secret, "[REDACTED]")
        value = re.sub(
            r"(?i)\b(OPENAI_API_KEY|DEEPSEEK_API_KEY)\s*=\s*([^\s,;]+)",
            r"\1=[REDACTED]",
            value,
        )
        value = re.sub(
            r"(?i)(\b--api-key\s+|\bBearer\s+)([^\s,;]+)",
            r"\1[REDACTED]",
            value,
        )
        return value

    @property
    def last_provider_usage(self) -> tuple[int, int] | None:
        """最近一次 run 的实际输入/输出用量，缺失时不伪装估算。"""
        return (self._input_tokens, self._output_tokens) if self._has_usage else None

    def handle(self, event: AgentEvent) -> None:
        if isinstance(event, AgentStartEvent):
            self._input_tokens = 0
            self._output_tokens = 0
            self._has_usage = False
            self.last_tool_count = 0
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
            action = _tool_action(event.tool_call.name, event.tool_call.arguments)
            if self._verbose:
                action += f" {_format_arguments(event.tool_call.arguments, limit=None)}"
            self.console.print(
                self._redact(f"● {action}"),
                style="cyan",
                markup=False,
                highlight=False,
            )
        elif isinstance(event, ToolExecutionEndEvent):
            self.last_tool_count += 1
            details = event.result.details or {}
            failed = event.is_error or (
                event.tool_call.name == "bash"
                and isinstance(details.get("exit_code"), int)
                and details["exit_code"] != 0
            )
            line = self._redact(_tool_result(event))
            self.console.print(
                f"{'✗' if failed else '✓'} {line}",
                style="red" if failed else "green",
                markup=False,
                highlight=False,
            )
            if self._verbose:
                # Tool 层已做有界截断；进程层丢弃的内容无法恢复。
                self.console.print(
                    self._redact(event.result.content),
                    style="dim",
                    markup=False,
                    highlight=False,
                )
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
