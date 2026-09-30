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
    "- After each observation, decide whether you already have enough evidence to answer. For simple read-only questions, keep inspection focused; investigate further when the task requires it.",
    "- When the task is complete, stop calling tools and summarize what changed and how it was verified.",
)
_INTERACTION_RULE = (
    "- When a necessary human decision about the approach, data migration, or a destructive "
    "action cannot be determined from the user's request or repository evidence, call ask_user "
    "alone in its tool batch and wait for the answer. Do not guess the decision or ask only "
    "in prose. Resolve ordinary implementation details yourself."
)


def build_sections(
    *,
    cwd: Path,
    tools: list[ToolSchema],
    project_instructions: Sequence[ProjectInstruction] = (),
) -> tuple[PromptSection, ...]:
    """按固定顺序构建 system prompt，并可追加已加载的项目规则。"""
    tool_lines = "\n".join(f"- {tool.name}: {tool.description}" for tool in tools)
    rules = _RULES
    if any(tool.name == "ask_user" for tool in tools):
        rules = _RULES[:-2] + (_INTERACTION_RULE,) + _RULES[-2:]
    sections = (
        PromptSection(id="preamble", content=_PREAMBLE),
        PromptSection(
            id="environment",
            content=(
                f"- Workspace root: {cwd}\n"
                f"- Platform: {platform.system().lower()}"
            ),
        ),
        PromptSection(id="rules", content="\n".join(rules)),
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
