"""CLI 本地状态与上下文展示；不修改消息或 Session。"""

from __future__ import annotations

from pathlib import Path

from rich.console import Console

from mini_pi.agent.agent import Agent
from mini_pi.cli.console import ConsoleRenderer
from mini_pi.context.policy import resolve_policy
from mini_pi.context.stats import ContextStats, context_stats
from mini_pi.session.runtime import AgentSession
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
    """展示模型、目录、会话和最近任务工具次数。"""
    session = agent.session_id[:8] if isinstance(agent, AgentSession) else "memory only"
    console.print(f"Model: {provider}/{model}", markup=False)
    console.print(f"Workspace: {cwd if full else cwd.name}", markup=False)
    console.print(f"Session: {session}", markup=False)
    if full and isinstance(agent, AgentSession):
        console.print(f"Session path: {agent.path}", markup=False, soft_wrap=True)
    console.print(f"Context: {_usage_label(_stats(agent).total, model)}", markup=False)
    console.print(f"Last run tools: {renderer.last_tool_count}", markup=False)


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
    usage = renderer.last_provider_usage
    if usage is not None:
        console.print(f"Last run Provider usage: in {usage[0]} / out {usage[1]}", markup=False)
    if resolve_policy(model) is None:
        console.print("Auto-compaction: unavailable (context window unknown)", markup=False)
    else:
        console.print("Auto-compaction: planned for M7.6", markup=False)


def render_tools(console: Console, *, agent: Agent | AgentSession | None, cwd: Path) -> None:
    """列出当前工具；尚未连接时展示此 workspace 的默认 Registry。"""
    schemas = agent.tool_schemas if agent is not None else build_default_registry(Workspace(cwd)).schemas()
    for schema in schemas:
        console.print(f"{schema.name}: {schema.description}", markup=False)
