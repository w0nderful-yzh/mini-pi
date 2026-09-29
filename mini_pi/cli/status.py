"""CLI 本地状态与上下文展示；不修改消息或 Session。"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from rich.console import Console

from mini_pi.agent.agent import Agent
from mini_pi.cli import style
from mini_pi.cli.console import (
    ConsoleRenderer,
    _preview,
    _tool_action,
    bounded_output,
    redact_text,
)
from mini_pi.context.policy import ContextPolicy
from mini_pi.context.stats import ContextStats, context_stats
from mini_pi.llm.types import Usage
from mini_pi.session.runtime import AgentSession, CompactionExecution
from mini_pi.session.usage import RunToolCall, RunUsage, recent_run_tools, recent_run_usage
from mini_pi.tools import build_default_registry
from mini_pi.workspace.workspace import Workspace


def _stats(agent: Agent | AgentSession | None) -> ContextStats:
    """只读当前投影；压缩后的历史原文不会重新计入。"""
    if agent is None:
        return context_stats(())
    summary_index = agent.summary_index if isinstance(agent, AgentSession) else None
    return context_stats(
        agent.state.messages, tools=agent.tool_schemas, summary_index=summary_index
    )


def _usage_label(total: int, policy: ContextPolicy | None) -> str:
    """窗口未知时不伪造百分比；已知窗口显示估算占用。"""
    if policy is None:
        return f"~{style.count(total)} tokens / window unknown"
    percentage = total / policy.context_window * 100
    return (
        f"~{style.count(total)} / {style.count(policy.context_window)} tokens ({percentage:.1f}%)"
    )


def _compaction_label(agent: Agent | AgentSession | None) -> str:
    """压缩配置一句话概括：阈值与窗口来源，成本触发细节留在 `/context`。"""
    policy = agent.context_policy if agent is not None else None
    if policy is None:
        return "unavailable (context window unknown)"
    return (
        f"auto at {style.count(policy.threshold_tokens)} tokens "
        f"(window {style.count(policy.context_window)})"
    )


def current_context_tokens(agent: Agent | AgentSession | None) -> int:
    """返回下一请求预测；无运行时对象时没有可发送的上下文。"""
    return agent.request_snapshot().input_tokens if agent is not None else 0


def _run_usage(agent: Agent | AgentSession | None, renderer: ConsoleRenderer) -> RunUsage | None:
    """持久化模式重放活动链；纯内存模式使用消息与运行时耗时。"""
    if isinstance(agent, AgentSession):
        return agent.last_run_usage
    if isinstance(agent, Agent):
        return recent_run_usage(
            agent.state.messages, duration_seconds=renderer.last_run_seconds
        )
    return None


def _provider_amount(usage: RunUsage) -> str:
    """实测用量本身；覆盖率与 partial 限定单独说明。"""
    amount = f"in {style.count(usage.input_tokens)} / out {style.count(usage.output_tokens)} tokens"
    coverage = f"{usage.measured_requests}/{usage.requests} requests"
    qualifier = "partial " if usage.measured_requests < usage.requests else ""
    return f"{qualifier}{amount} ({coverage})"


def _provider_label(usage: RunUsage | None) -> str:
    """缺 usage 时显示覆盖率，不能把部分实测称为任务总成本。"""
    if usage is None or usage.requests == 0:
        return "unavailable (no model requests)"
    if usage.measured_requests == 0:
        return f"unavailable (0/{usage.requests} requests reported usage)"
    return _provider_amount(usage)


def render_status(
    console: Console,
    *,
    agent: Agent | AgentSession | None,
    provider: str,
    model: str,
    cwd: Path,
    renderer: ConsoleRenderer,
    full: bool = False,
) -> None:
    """默认只给决策所需信息；请求/工具/耗时明细留给 `/status full`。"""
    session = agent.session_id[:8] if isinstance(agent, AgentSession) else "memory only"
    console.print(style.row("Model", f"{provider}/{model}"), markup=False, highlight=False, soft_wrap=not console.is_terminal)
    console.print(style.row("Workspace", str(cwd) if full else cwd.name), markup=False, highlight=False, soft_wrap=not console.is_terminal)
    console.print(style.row("Session", session), markup=False, highlight=False, soft_wrap=not console.is_terminal)
    if full and isinstance(agent, AgentSession):
        console.print(style.row("Session path", str(agent.path)), markup=False, highlight=False, soft_wrap=not console.is_terminal)
    console.print(
        style.row(
            "Context",
            _usage_label(current_context_tokens(agent), agent.context_policy if agent else None),
        ),
        markup=False,
        highlight=False,
        soft_wrap=not console.is_terminal,
    )
    usage = _run_usage(agent, renderer)
    console.print(style.row("Last run", _provider_label(usage)), markup=False, highlight=False, soft_wrap=not console.is_terminal)
    if full and usage is not None:
        console.print(style.row("Last tools", str(usage.tool_calls)), markup=False, highlight=False)
        if usage.duration_seconds is not None:
            label = "recorded span" if isinstance(agent, AgentSession) else "elapsed"
            console.print(
                style.row("Last span", f"{usage.duration_seconds:.1f}s ({label})"), markup=False
            )
    budget = agent.max_run_input_tokens if agent is not None else None
    console.print(
        style.row(
            "Budget",
            "disabled"
            if budget is None
            else f"{style.count(budget)} tokens (request-boundary, resets per task)",
        ),
        markup=False,
        highlight=False,
        soft_wrap=not console.is_terminal,
    )
    console.print(style.row("Compaction", _compaction_label(agent)), markup=False, highlight=False, soft_wrap=not console.is_terminal)


def render_context(
    console: Console,
    *,
    agent: Agent | AgentSession | None,
    renderer: ConsoleRenderer,
) -> None:
    """按当前投影显示分类估算，实测 Provider 用量单独成组。"""
    stats = _stats(agent)
    for label, amount in (
        ("System / rules", stats.system),
        ("AGENTS.md", stats.agents),
        ("Conversation", stats.conversation),
        ("Tool results", stats.tool_results),
        ("Summaries", stats.summaries),
        ("Tool schemas", stats.tools),
    ):
        console.print(style.row(label, f"~{style.count(amount)} tokens"), markup=False, highlight=False)
    policy = agent.context_policy if agent is not None else None
    console.print(style.row("Total", _usage_label(stats.total, policy)), markup=False, highlight=False, soft_wrap=not console.is_terminal)

    console.print()
    console.print(
        style.row("Next request", f"~{style.count(current_context_tokens(agent))} tokens"),
        markup=False,
    )
    usage = _run_usage(agent, renderer)
    latest = usage.latest_input_tokens if usage is not None else None
    console.print(
        style.row(
            "Last request",
            f"in {style.count(latest)} tokens"
            if latest is not None
            else "unavailable (provider returned no usage)",
        ),
        markup=False,
    )
    console.print(style.row("Last run", _provider_label(usage)), markup=False, highlight=False, soft_wrap=not console.is_terminal)

    console.print()
    if policy is None:
        console.print(
            style.row("Compaction", "unavailable (context window unknown)"), markup=False
        )
        return
    console.print(
        style.row(
            "Compaction",
            f"window threshold {style.count(policy.threshold_tokens)} tokens "
            f"(window {style.count(policy.context_window)} - reserve {style.count(policy.reserve_tokens)})",
        ),
        markup=False,
        highlight=False,
        soft_wrap=not console.is_terminal,
    )
    # 窗口阈值与成本触发是两条独立路径，第二行只说明后者是否生效
    console.print(
        style.hanging(
            "cost-aware early compaction for old tool results is enabled"
            if isinstance(agent, AgentSession)
            else "automatic compaction unavailable (saved session required)"
        ),
        markup=False,
        highlight=False,
        style=style.MUTED,
        soft_wrap=not console.is_terminal,
    )


def _summary_usage_label(usage: Usage | None) -> str:
    """摘要调用的实测用量；Provider 未返回时明确标注不可用，不伪造成本。"""
    if usage is None:
        return "unavailable (provider returned no usage)"
    return f"in {style.count(usage.input_tokens)} / out {style.count(usage.output_tokens)} tokens"


def render_compaction(
    console: Console,
    *,
    policy: ContextPolicy | None,
    execution: CompactionExecution,
    tokens_before: int,
    tokens_after: int,
) -> None:
    """显示压缩前后估算、切点与摘要调用成本；跳过时只说明原因。"""
    result = execution.result
    if result is None:
        console.print(
            style.row("Compaction", f"skipped: {execution.reason}"), markup=False, highlight=False, soft_wrap=not console.is_terminal
        )
        return
    plan = result.plan
    console.print(
        style.row(
            "Compaction",
            f"summarized {len(plan.messages_to_summarize)} messages, "
            f"kept {len(plan.kept_entry_ids)} entries (cut boundary: {plan.cut.boundary})",
        ),
        markup=False,
        highlight=False,
        soft_wrap=not console.is_terminal,
    )
    console.print(
        style.row(
            "Context",
            f"~{style.count(tokens_before)} → {_usage_label(tokens_after, policy)}",
        ),
        markup=False,
        highlight=False,
        soft_wrap=not console.is_terminal,
    )
    console.print(
        style.row("Summary usage", _summary_usage_label(result.usage)), markup=False, highlight=False, soft_wrap=not console.is_terminal
    )


def _last_run_tools(agent: Agent | AgentSession | None) -> tuple[RunToolCall, ...]:
    """最近一次任务的工具调用；持久化会话走活动链，纯内存模式走内存消息。"""
    if isinstance(agent, AgentSession):
        return agent.last_run_tools
    if isinstance(agent, Agent):
        return recent_run_tools(agent.state.messages)
    return ()


def _shell_exit_code(content: str) -> int | None:
    """从 bash observation 首行读回退出码；格式不符时返回 None，不猜。"""
    match = re.match(r"exit_code:\s*(-?\d+)", content)
    return int(match.group(1)) if match else None


def _tool_result_line(item: RunToolCall) -> str:
    """按落盘事实还原一行工具结果；details 不持久化，因此不假装有它。"""
    result = item.result
    exit_code = _shell_exit_code(result.content) if result.name == "bash" else None
    # details 缺失时用 observation 首行判断 shell 失败，与实时视图保持同一结论
    failed = result.is_error or (exit_code is not None and exit_code != 0)
    mark = style.MARK_FAILED if failed else style.MARK_OK
    title = _tool_action(item.call.name, item.call.arguments)
    if failed or result.name == "bash":
        # bash 首行固定是 exit_code（含超时标记）；错误的首行就是原因
        detail = _preview(result.content, limit=90)
    elif result.modified_files:
        detail = f"{len(result.modified_files)} file(s) changed"
    else:
        detail = "completed"
    if not failed and (
        "[output truncated]" in result.content or "[Showing lines " in result.content
    ):
        detail += " (truncated)"
    return f"{mark} {title} · {detail}"


def render_last_run(
    console: Console,
    *,
    agent: Agent | AgentSession | None,
    full: bool = False,
    index: int | None = None,
    page: int = 1,
    redact: Callable[[str], str] = redact_text,
) -> None:
    """最近一次任务按页列摘要；单项可展开已捕获的完整 observation。"""
    items = _last_run_tools(agent)
    if not items:
        console.print("Last run tool calls: none", markup=False, highlight=False)
        return
    if index is not None and not 1 <= index <= len(items):
        console.print(f"/last index must be 1..{len(items)}", markup=False, highlight=False)
        return
    page_size = 20
    pages = (len(items) + page_size - 1) // page_size
    if index is None and not 1 <= page <= pages:
        console.print(f"/last page must be 1..{pages}", markup=False, highlight=False)
        return
    if index is not None:
        selected = [(index, items[index - 1])]
        console.print(
            f"Last run tool call {index}/{len(items)}:", markup=False, highlight=False
        )
    else:
        start = (page - 1) * page_size
        selected = list(enumerate(items[start : start + page_size], start=start + 1))
        console.print(
            f"Last run tool calls ({len(items)}) · page {page}/{pages}:",
            markup=False,
            highlight=False,
        )
    for number, item in selected:
        console.print(
            redact(f"{number:>2}. {_tool_result_line(item)}"),
            markup=False,
            highlight=False,
            soft_wrap=not console.is_terminal,
        )
        if full or index is not None:
            # 批量 full 只预览每项；单项 full 才输出工具已捕获的全部字节。
            content = redact(item.result.content)
            shown, shortened = (
                (content, False)
                if index is not None and full
                else bounded_output(content, lines=3 if index is None else 20, chars=300 if index is None else 2000)
            )
            console.print(
                style.hanging(shown),
                style=style.MUTED,
                markup=False,
                highlight=False,
                soft_wrap=not console.is_terminal,
            )
            if shortened:
                console.print(
                    style.hanging(f"… display shortened; /last {number} full shows captured content"),
                    style=style.MUTED,
                    markup=False,
                    highlight=False,
                    soft_wrap=not console.is_terminal,
                )
    if index is None and page < pages:
        console.print(
            f"More tool calls: /last page {page + 1}",
            style=style.MUTED,
            markup=False,
            highlight=False,
        )


def render_tools(console: Console, *, agent: Agent | AgentSession | None, cwd: Path) -> None:
    """列出当前工具；尚未连接时展示此 workspace 的默认 Registry。"""
    schemas = agent.tool_schemas if agent is not None else build_default_registry(Workspace(cwd)).schemas()
    width = max((len(schema.name) for schema in schemas), default=0) + 2
    for schema in schemas:
        console.print(
            f"{schema.name:<{width}}{schema.description}", markup=False, highlight=False, soft_wrap=not console.is_terminal
        )
