"""System prompt 组装。"""

from __future__ import annotations

import platform
from pathlib import Path

from mini_pi.context.sections import PromptSection, render_sections
from mini_pi.llm.types import ToolSchema

_PREAMBLE = "You are mini-pi, a coding agent working inside a local workspace."
_RULES = (
    "- All paths are resolved inside the workspace; paths outside the workspace are rejected.",
    "- Before changing code, locate it with the search tool and read it with the read tool. Never guess file contents.",
    "- Prefer the edit tool for minimal changes; use the write tool only for new files or full rewrites.",
    "- Verify changes with the bash tool (tests/build/lint) and inspect diffs with git_diff.",
    "- Tool errors are returned to you as error observations; read them and adjust instead of repeating the same call.",
    "- When the task is complete, stop calling tools and summarize what changed and how it was verified.",
)
def build_sections(*, cwd: Path, tools: list[ToolSchema]) -> tuple[PromptSection, ...]:
    """以固定顺序构建当前静态 system prompt sections。"""
    tool_lines = "\n".join(f"- {tool.name}: {tool.description}" for tool in tools)
    return (
        PromptSection(id="preamble", content=_PREAMBLE),
        PromptSection(
            id="environment",
            content=(
                f"- Workspace root: {cwd}\n"
                f"- Platform: {platform.system().lower()}"
            ),
        ),
        PromptSection(id="rules", content="\n".join(_RULES)),
        PromptSection(id="tools", content=tool_lines),
    )


def build_system_prompt(*, cwd: Path, tools: list[ToolSchema]) -> str:
    """渲染最小 system prompt，保持 Phase 1 对外接口与文本格式。"""
    return render_sections(build_sections(cwd=cwd, tools=tools))
