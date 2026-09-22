"""CLI 渲染器：AgentEvent -> Rich 终端输出。"""

from __future__ import annotations

import json
import re
import shlex
from importlib import resources
from pathlib import Path
from time import perf_counter

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

_SHELL_CONTROL = re.compile(r"[;&|<>`\r\n]|\$\(|\$\{")
_SENSITIVE_COMMAND = re.compile(
    r"(?i)(?:api[_-]?key|authorization|bearer|password|secret|token|credential)"
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


def _single_line(value: str, *, limit: int = 200) -> str:
    """默认事件只占一行；先由调用方脱敏，再清除控制字符并限制长度。"""
    clean = " ".join("".join(char if char.isprintable() else " " for char in value).split())
    return clean[: limit - 1] + "…" if len(clean) > limit else clean


def _rg_pattern(tokens: list[str]) -> str | None:
    """仅识别无需推断参数值的常见 rg 选项，复杂形式保持通用标题。"""
    flags = {"-n", "-i", "-F", "--line-number", "--ignore-case", "--fixed-strings", "--hidden"}
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token in flags:
            index += 1
        elif token in {"-g", "--glob"}:
            index += 2
        elif token.startswith("-"):
            return None
        else:
            return token
    return None


def _shell_action(command: str) -> str:
    """只按确定的简单命令形态给标题，不解释管道或含凭据脚本。"""
    if _SENSITIVE_COMMAND.search(command):
        return "Run shell command (credentials hidden)"
    if _SHELL_CONTROL.search(command):
        return "Run shell pipeline" if "|" in command else "Run compound shell command"
    try:
        tokens = shlex.split(command)
    except ValueError:
        return "Run shell command"
    if not tokens:
        return "Run shell command"
    name = Path(tokens[0]).name.lower()
    words = [item.lower() for item in tokens]
    if name == "git" and len(words) > 1:
        return {
            "status": "Inspect git status",
            "diff": "Inspect git diff",
            "log": "Inspect git history",
            "add": "Stage git changes",
            "commit": "Create git commit",
            "push": "Push git commits",
            "merge": "Merge git branches",
        }.get(words[1], "Run git command")
    if name == "rg":
        if "--files" in words:
            return "List repository files with rg"
        pattern = _rg_pattern(tokens)
        return f"Search {pattern!r} with rg" if pattern is not None else "Search files with rg"
    if name == "pytest" or words[:3] in (["python", "-m", "pytest"], ["python3", "-m", "pytest"]):
        return "Run pytest"
    if words[:3] == ["uv", "run", "pytest"] or words[:5] == ["uv", "run", "python", "-m", "pytest"]:
        return "Run pytest with uv"
    if name in {"npm", "pnpm", "yarn", "bun"}:
        task = words[2] if len(words) > 2 and words[1] == "run" else (words[1] if len(words) > 1 else "")
        if task == "build":
            return f"Run frontend build ({name})"
        if task == "test":
            return f"Run frontend tests ({name})"
    if name in {"ls", "find"}:
        return f"List files with {name}"
    if name in {"cat", "sed"}:
        return f"Inspect file with {name}"
    # 未识别的命令仅展示程序名；参数可能包含路径、凭据或复杂语义。
    return f"Run {name}" if re.fullmatch(r"[a-z0-9_.+-]+", name) else "Run shell command"


def _tool_action(name: str, arguments: dict[str, object]) -> str:
    """默认展示工具意图，不输出整段参数或文件内容。"""
    path = arguments.get("path")
    if name in {"read", "write", "edit"} and isinstance(path, str):
        return f"{name.capitalize()} {path}"
    if name == "search":
        pattern = arguments.get("pattern")
        if isinstance(pattern, str) and not _SENSITIVE_COMMAND.search(pattern):
            return f"Search {pattern!r} in {path or 'workspace'}"
        return "Search workspace"
    if name == "bash":
        command = arguments.get("command")
        return _shell_action(command) if isinstance(command, str) else "Run shell command"
    if name == "git_diff":
        return f"Inspect git diff for {path}" if isinstance(path, str) else "Inspect git diff"
    return name


def _stderr_preview(content: str) -> str | None:
    """只从 BashTool 的固定 observation 格式提取 stderr 首条有效信息。"""
    marker = "\nstderr:\n"
    if marker not in content:
        return None
    stderr = content.rsplit(marker, 1)[1].split("\n[output truncated]", 1)[0]
    preview = _preview(stderr, limit=120)
    return None if preview == "(empty)" else preview


def _tool_result(event: ToolExecutionEndEvent, *, content: str | None = None) -> str:
    """摘要保留退出码、超时、截断和改动数量等关键结果。"""
    details = event.result.details or {}
    observation = event.result.content if content is None else content
    if event.is_error:
        line = f"failed: {_preview(observation)}"
    elif event.tool_call.name == "bash":
        code = details.get("exit_code")
        line = f"shell exited {code}" if isinstance(code, int) and not isinstance(code, bool) else "shell result unavailable"
        if details.get("timed_out"):
            line += " (timed out)"
        if details.get("stdout_truncated") or details.get("stderr_truncated"):
            line += " (output truncated)"
        if isinstance(code, int) and code != 0:
            stderr = _stderr_preview(observation)
            if stderr is not None:
                line += f" · stderr: {stderr}"
    elif event.tool_call.name == "search" and isinstance(details.get("count"), int):
        line = f"{details['count']} matches"
    elif event.tool_call.name in {"read", "write", "edit", "git_diff"}:
        line = "completed"
    else:
        line = _preview(observation)
    if event.tool_call.name in {"read", "git_diff", "search"} and (
        "[output truncated]" in observation
        or "[Truncated at " in observation
        or "[Showing lines " in observation
    ):
        line += " (output truncated)"
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
        self._requests = 0
        self._measured_requests = 0
        self._started_at: float | None = None
        self.last_run_seconds: float | None = None
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
            r"(?i)((?<!\w)--api-key(?:\s+|=)|\bBearer\s+)([^\s,;]+)",
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
            self._requests = 0
            self._measured_requests = 0
            self._started_at = perf_counter()
            self.last_run_seconds = None
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
            self._requests += 1
            if usage is not None:
                # usage 是否存在与数值是否大于零是两件事；零值也属于已报告。
                self._input_tokens += usage.input_tokens
                self._output_tokens += usage.output_tokens
                self._has_usage = True
                self._measured_requests += 1
        elif isinstance(event, ToolExecutionStartEvent):
            self._stop_thinking()
            action = _tool_action(event.tool_call.name, event.tool_call.arguments)
            if self._verbose:
                action += f" {_format_arguments(event.tool_call.arguments, limit=None)}"
            else:
                action = _single_line(self._redact(action))
            self.console.print(
                self._redact(f"● {action}"),
                style="cyan",
                markup=False,
                highlight=False,
                soft_wrap=True,
            )
        elif isinstance(event, ToolExecutionEndEvent):
            self.last_tool_count += 1
            details = event.result.details or {}
            failed = event.is_error or (
                event.tool_call.name == "bash"
                and isinstance(details.get("exit_code"), int)
                and details["exit_code"] != 0
            )
            # 先对完整 observation 脱敏，再做预览截断，避免长 Key 泄漏前缀。
            line = _tool_result(event, content=self._redact(event.result.content))
            if not self._verbose:
                line = _single_line(line)
            self.console.print(
                f"{'✗' if failed else '✓'} {line}",
                style="red" if failed else "green",
                markup=False,
                highlight=False,
                soft_wrap=True,
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
            if self._started_at is not None:
                self.last_run_seconds = perf_counter() - self._started_at
            self._render_end(event)
            usage = (
                f"in {self._input_tokens} / out {self._output_tokens}"
                if self._has_usage
                else "unavailable"
            )
            if 0 < self._measured_requests < self._requests:
                usage = f"partial {usage}"
            coverage = f"{self._measured_requests}/{self._requests} usage"
            elapsed = f" · {self.last_run_seconds:.1f}s" if self.last_run_seconds is not None else ""
            self.console.print(
                f"  requests {self._requests} · provider {usage} ({coverage})"
                f" · tools {self.last_tool_count}{elapsed}",
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
