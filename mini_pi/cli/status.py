"""CLI 本地状态与上下文展示；不修改消息或 Session。"""

from __future__ import annotations

from pathlib import Path

from rich.console import Console

from mini_pi.agent.agent import Agent
from mini_pi.cli.console import ConsoleRenderer
from mini_pi.context.policy import resolve_policy
from mini_pi.context.stats import ContextStats, context_stats
from mini_pi.session.runtime import AgentSession
from mini_pi.session.usage import RunUsage, recent_run_usage
from mini_pi.tools import build_default_registry
from mini_pi.workspace.workspace import Workspace


def _stats(agent: Agent | AgentSession | None) -> ContextStats:
    """只读当前投影；压缩后的历史原文不会重新计入。"""
    if agent is None:
        return context_stats(())
    summary_index = agent.summary_index if isinstance(agent, AgentSession) else None
    return context_stats(agent.state.messages, summary_index=summary_index)


def _usage_label(total: int, model: str) -> str:
    """窗口未知时不伪造百分比；已知窗口显示估算占用。"""
    policy = resolve_policy(model)
    if policy is None:
        return f"~{total} tokens / window unknown"
    percentage = total / policy.context_window * 100
    return f"~{total} / {policy.context_window} tokens ({percentage:.1f}%)"


def _run_usage(agent: Agent | AgentSession | None, renderer: ConsoleRenderer) -> RunUsage | None:
    """持久化模式重放活动链；纯内存模式使用消息与运行时耗时。"""
    if isinstance(agent, AgentSession):
        return agent.last_run_usage
    if isinstance(agent, Agent):
        return recent_run_usage(
            agent.state.messages, duration_seconds=renderer.last_run_seconds
        )
    return None


def _provider_label(usage: RunUsage | None) -> str:
    """缺 usage 时显示覆盖率，不能把部分实测称为任务总成本。"""
    if usage is None or usage.requests == 0:
        return "unavailable (no model requests)"
    if usage.measured_requests == 0:
        return f"unavailable (0/{usage.requests} requests reported usage)"
    amount = f"in {usage.input_tokens} / out {usage.output_tokens} tokens"
    coverage = f"{usage.measured_requests}/{usage.requests} requests reported usage"
    qualifier = "partial measured: " if usage.measured_requests < usage.requests else ""
    return f"{qualifier}{amount} ({coverage})"


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
    """分开展示最近任务累计消耗与当前模型投影。"""
    session = agent.session_id[:8] if isinstance(agent, AgentSession) else "memory only"
    console.print(f"Model: {provider}/{model}", markup=False)
    console.print(f"Workspace: {cwd if full else cwd.name}", markup=False)
    console.print(f"Session: {session}", markup=False)
    if full and isinstance(agent, AgentSession):
        console.print(f"Session path: {agent.path}", markup=False, soft_wrap=True)
    console.print(f"Current context (estimated): {_usage_label(_stats(agent).total, model)}", markup=False)
    usage = _run_usage(agent, renderer)
    console.print(f"Last run Provider usage: {_provider_label(usage)}", markup=False)
    if usage is not None:
        console.print(f"Last run requests: {usage.requests}", markup=False)
        console.print(f"Last run tools: {usage.tool_calls}", markup=False)
        if usage.duration_seconds is not None:
            label = "recorded span" if isinstance(agent, AgentSession) else "elapsed"
            console.print(f"Last run {label}: {usage.duration_seconds:.1f}s", markup=False)


def render_context(
    console: Console,
    *,
    agent: Agent | AgentSession | None,
    model: str,
    renderer: ConsoleRenderer,
) -> None:
    """按当前投影显示分类估算，实际 Provider 用量单独列出。"""
    stats = _stats(agent)
    for label, amount in (
        ("System / rules", stats.system),
        ("AGENTS.md", stats.agents),
        ("Conversation", stats.conversation),
        ("Tool results", stats.tool_results),
        ("Summaries", stats.summaries),
    ):
        console.print(f"{label}: ~{amount} tokens", markup=False)
    console.print(f"Total (estimated): {_usage_label(stats.total, model)}", markup=False)
    usage = _run_usage(agent, renderer)
    console.print(f"Last run Provider usage: {_provider_label(usage)}", markup=False)
    latest = usage.latest_input_tokens if usage is not None else None
    console.print(
        f"Last request Provider input: {latest if latest is not None else 'unavailable'}"
        + (" tokens" if latest is not None else ""),
        markup=False,
    )
    if resolve_policy(model) is None:
        console.print("Auto-compaction: unavailable (context window unknown)", markup=False)
    else:
        console.print("Auto-compaction: planned for M7.6", markup=False)


def render_tools(console: Console, *, agent: Agent | AgentSession | None, cwd: Path) -> None:
    """列出当前工具；尚未连接时展示此 workspace 的默认 Registry。"""
    schemas = agent.tool_schemas if agent is not None else build_default_registry(Workspace(cwd)).schemas()
    for schema in schemas:
        console.print(f"{schema.name}: {schema.description}", markup=False)
