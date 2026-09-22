"""System prompt 组装。"""

from __future__ import annotations

import platform
from collections.abc import Sequence
from pathlib import Path
from xml.sax.saxutils import quoteattr

from mini_pi.context.project import ProjectInstruction
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


def build_sections(
    *,
    cwd: Path,
    tools: list[ToolSchema],
    project_instructions: Sequence[ProjectInstruction] = (),
) -> tuple[PromptSection, ...]:
    """按固定顺序构建 system prompt，并可追加已加载的项目规则。"""
    tool_lines = "\n".join(f"- {tool.name}: {tool.description}" for tool in tools)
    sections = (
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
    if not project_instructions:
        return sections
    return sections + (
        PromptSection(
            id="project_context",
            content="\n\n".join(_render_project_instruction(item) for item in project_instructions),
        ),
    )


def _render_project_instruction(item: ProjectInstruction) -> str:
    """保留规则原文，并给缺少末尾换行的文件补齐 XML 边界。"""
    content = item.content if item.content.endswith("\n") else item.content + "\n"
    # 来源路径进入 XML 属性，必须加引号并转义特殊字符。
    return (
        f"<project_instructions path={quoteattr(item.path)}>\n"
        f"{content}</project_instructions>"
    )


def build_system_prompt(*, cwd: Path, tools: list[ToolSchema]) -> str:
    """渲染最小 system prompt，保持 Phase 1 对外接口与文本格式。"""
    return render_sections(build_sections(cwd=cwd, tools=tools))
