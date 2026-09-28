"""CLI 渲染器：AgentEvent -> Rich 终端输出。"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from time import perf_counter

from rich.cells import cell_len
from rich.console import Console
from rich.live import Live

from mini_pi.agent.events import (
    AgentEndEvent,
    AgentEndReason,
    AgentEvent,
    AgentStartEvent,
    BudgetWarningEvent,
    MessageDeltaEvent,
    MessageEndEvent,
    MessageStartEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
)
from mini_pi.cli import style
from mini_pi.llm.types import ToolCall

# 终止原因在收尾统计行里的说法；具体解释由 _render_end 单独给出
_OUTCOME_WORDS: dict[str, str] = {
    "completed": "Completed",
    "step_limit": "Stopped at step limit",
    "budget_limit": "Stopped at input budget",
    "error": "Failed",
    "cancelled": "Cancelled",
}

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
    if name == "git_status":
        return "Inspect git status"
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
    elif event.tool_call.name == "git_status" and isinstance(details.get("paths"), int):
        line = f"{details['paths']} changed path(s)"
    elif event.tool_call.name in {"read", "write", "edit", "git_diff"}:
        line = "completed"
    else:
        line = _preview(observation)
    if event.tool_call.name in {"read", "git_diff", "git_status", "search"} and (
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
        """初始化渲染器与本次 run 的用量/计时累加字段。"""
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
        self.last_end_reason: AgentEndReason | None = None
        self.last_tool_count = 0

    def _start_thinking(self) -> None:
        """只在交互终端显示单行帧动画；图案不写进日志，也不在滚动区留下残影。"""
        if not self._show_thinking or not self.console.is_terminal:
            return
        # 单行固定宽度是硬约束：多行图案在高度不足时会被裁剪，清除时光标回退量与
        # 屏幕实际内容不一致，图案就残留成刷屏（M7.9.4 那版 18 行 ASCII 牛即如此）
        if self.console.width < cell_len(style.thinking_line(0.0)):
            return
        started = perf_counter()
        self._thinking = Live(
            get_renderable=lambda: style.thinking_line(perf_counter() - started),
            console=self.console,
            auto_refresh=True,
            refresh_per_second=style.THINKING_FPS,
            transient=True,
            vertical_overflow="crop",
        )
        # 立刻画出第一帧，避免等第一个刷新周期时出现空白
        self._thinking.start(refresh=True)

    def _print(self, *args: object, **kwargs: object) -> None:
        """事件行统一出口：tty 里按终端宽度折行，非 tty 保持单行确定性输出。"""
        kwargs.setdefault("soft_wrap", not self.console.is_terminal)
        self.console.print(*args, **kwargs)

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
        """按 AgentEvent 类型渲染进度/工具/用量；纯消费事件不做决策。"""
        if isinstance(event, AgentStartEvent):
            self._input_tokens = 0
            self._output_tokens = 0
            self._has_usage = False
            self._requests = 0
            self._measured_requests = 0
            self._started_at = perf_counter()
            self.last_run_seconds = None
            self.last_end_reason = None
            self.last_tool_count = 0
        elif isinstance(event, MessageStartEvent):
            self._start_thinking()
        elif isinstance(event, MessageDeltaEvent):
            if event.kind == "thinking":
                return
            self._stop_thinking()
            self._print(event.delta, end="", markup=False, highlight=False)
            self._printing_text = True
        elif isinstance(event, MessageEndEvent):
            self._stop_thinking()
            if self._printing_text:
                self._print()
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
            self._print(
                self._event_line(f"{style.MARK_RUNNING} {self._tool_title(event.tool_call)}"),
                style=style.RUNNING,
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
            # 先对完整 observation 脱敏，再做预览截断，避免长 Key 泄漏前缀。
            result = _tool_result(event, content=self._redact(event.result.content))
            # 结束行重复工具标题：长会话里单看 "✓ completed" 无法判断是哪个操作
            line = self._event_line(
                f"{self._tool_title(event.tool_call)} · {result}"
            )
            self._print(
                f"{style.MARK_FAILED if failed else style.MARK_OK} {line}",
                style=style.FAILURE if failed else style.SUCCESS,
                markup=False,
                highlight=False,
            )
            if self._verbose:
                # Tool 层已做有界截断；进程层丢弃的内容无法恢复。
                self._print(
                    self._redact(event.result.content),
                    style=style.MUTED,
                    markup=False,
                    highlight=False,
                )
        elif isinstance(event, AgentEndEvent):
            self._stop_thinking()
            self.last_end_reason = event.reason
            if self._started_at is not None:
                self.last_run_seconds = perf_counter() - self._started_at
            self._render_end(event)
            self._print(
                self._summary_line(event.reason),
                style=style.MUTED,
                markup=False,
                highlight=False,
            )
        elif isinstance(event, BudgetWarningEvent):
            qualifier = "estimated" if event.source == "estimated" else event.source
            self._print(
                f"{style.MARK_WARNING} Run input budget is close: "
                f"{style.count(event.used)}/{style.count(event.limit)} used ({qualifier}); "
                f"next request ~{style.count(event.predicted_next_input)}, "
                f"{style.count(event.remaining)} remaining. Asking the model to conclude if possible.",
                style=style.WARNING,
                markup=False,
                highlight=False,
            )

    def _tool_title(self, tool_call: ToolCall) -> str:
        """工具标题（verbose 附带完整参数）；脱敏与单行化由 _event_line 统一处理。"""
        title = _tool_action(tool_call.name, tool_call.arguments)
        if self._verbose:
            return f"{title} {_format_arguments(tool_call.arguments, limit=None)}"
        return title

    def _event_line(self, value: str) -> str:
        """事件行统一处理：先脱敏再转单行；verbose 只脱敏，交给终端软换行。"""
        redacted = self._redact(value)
        return redacted if self._verbose else _single_line(redacted)

    def _summary_line(self, reason: AgentEndReason) -> str:
        """收尾统计行：结果词 · 工具数 · 请求数 · Provider 用量 · 耗时。"""
        if self._has_usage:
            usage = f"in {style.count(self._input_tokens)} / out {style.count(self._output_tokens)}"
            if 0 < self._measured_requests < self._requests:
                usage += f" (partial {self._measured_requests}/{self._requests})"
        else:
            usage = "provider usage unavailable"
        parts = [
            _OUTCOME_WORDS.get(reason, reason),
            f"{self.last_tool_count} tools",
            f"{self._requests} requests",
            usage,
        ]
        if self.last_run_seconds is not None:
            parts.append(f"{self.last_run_seconds:.1f}s")
        return " · ".join(parts)

    def _render_end(self, event: AgentEndEvent) -> None:
        """根据终止原因输出结束提示（completed/step_limit/error/budget_limit/cancelled）。"""
        if event.reason == "step_limit":
            # 步数用尽不是完成：说明上限值与下一步动作，避免把最后一条正文当成最终回答
            limit = f" ({event.step_limit})" if event.step_limit is not None else ""
            self._print(
                f"{style.MARK_WARNING} Reached the step limit{limit} before finishing the task. "
                "The task is incomplete; continue in this session or raise --max-steps.",
                style=style.WARNING,
                markup=False,
                highlight=False,
            )
        elif event.reason == "cancelled":
            # 中断不是完成：明确说明保留了什么，避免把 Ctrl+C 当成任务成功
            self._print(
                f"{style.MARK_FAILED} Task cancelled by user. "
                "Committed messages and file changes were kept.",
                style=style.FAILURE,
                markup=False,
                highlight=False,
            )
        elif event.reason == "error":
            self._print(
                f"{style.MARK_FAILED} Agent stopped with an error: {event.error}",
                style=style.FAILURE,
                markup=False,
                highlight=False,
            )
        elif event.reason == "budget_limit":
            source = event.budget_source or "estimated"
            self._print(
                f"{style.MARK_WARNING} Run stopped before the next model request: "
                "input budget would be exceeded "
                f"({style.count(event.budget_used or 0)}/{style.count(event.budget_limit or 0)} used, "
                f"next request ~{style.count(event.predicted_next_input or 0)}, {source}). "
                "The task is incomplete; continue in this session to start a new run budget.",
                style=style.WARNING,
                markup=False,
                highlight=False,
            )
