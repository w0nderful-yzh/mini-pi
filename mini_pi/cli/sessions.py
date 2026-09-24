"""CLI Session 列表：只读严格校验后的当前 workspace 元数据。"""

from __future__ import annotations

from datetime import timezone
from pathlib import Path

from rich.console import Console

from mini_pi.session.jsonl import list_session_summaries


def render_sessions(
    console: Console,
    *,
    cwd: Path,
    current_session_id: str | None,
) -> None:
    """显示短 id、活动时间、活动模型与摘要状态，不读取或调用 LLM。"""
    summaries = list_session_summaries(cwd)
    project = cwd.name or str(cwd)
    if not summaries:
        console.print(f"No saved sessions for {project}.", markup=False)
        return

    console.print(f"Saved sessions for {project}:", markup=False)
    for item in summaries:
        marker = "*" if str(item.id) == current_session_id else " "
        active = item.activity_time.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
        console.print(
            f"{marker} {str(item.id)[:8]} · {active} · "
            f"{item.provider}/{item.model} · summary {'yes' if item.has_summary else 'no'}",
            markup=False,
            highlight=False,
        )
    if current_session_id is not None:
        console.print("* current session", style="dim", markup=False, highlight=False)
    console.print(
        "--continue resumes the latest validated session; "
        "--resume <session.jsonl> resumes an exact file",
        style="dim",
        markup=False,
        highlight=False,
    )
