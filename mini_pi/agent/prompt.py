"""System prompt 组装。"""

from __future__ import annotations

import platform
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from mini_pi.llm.types import SystemPromptSectionId, ToolSchema

_PREAMBLE = "You are mini-pi, a coding agent working inside a local workspace."
_RULES = (
    "- All paths are resolved inside the workspace; paths outside the workspace are rejected.",
    "- Before changing code, locate it with the search tool and read it with the read tool. Never guess file contents.",
    "- Prefer the edit tool for minimal changes; use the write tool only for new files or full rewrites.",
    "- Verify changes with the bash tool (tests/build/lint) and inspect diffs with git_diff.",
    "- Tool errors are returned to you as error observations; read them and adjust instead of repeating the same call.",
    "- When the task is complete, stop calling tools and summarize what changed and how it was verified.",
)
_SECTION_TITLES: dict[SystemPromptSectionId, str | None] = {
    "preamble": None,
    "environment": "Environment",
    "rules": "Working rules",
    "tools": "Tools",
    "project_context": "Project Context",
}


@dataclass(frozen=True, slots=True)
class PromptSection:
    """一段可独立识别且顺序稳定的 system prompt 内容。"""

    id: SystemPromptSectionId
    content: str


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


def render_sections(sections: Sequence[PromptSection]) -> str:
    """用统一标题边界渲染 sections，保留既有 prompt 文本格式。"""
    blocks: list[str] = []
    for section in sections:
        title = _SECTION_TITLES[section.id]
        if title is None:
            blocks.append(section.content)
        else:
            # 空 section 仍保留标题，确保工具为空时与旧 prompt 字节级兼容。
            blocks.append(f"# {title}\n{section.content}")
    if not blocks:
        return ""
    return "\n\n".join(blocks) + "\n"


def build_system_prompt(*, cwd: Path, tools: list[ToolSchema]) -> str:
    """渲染最小 system prompt，保持 Phase 1 对外接口与文本格式。"""
    return render_sections(build_sections(cwd=cwd, tools=tools))
