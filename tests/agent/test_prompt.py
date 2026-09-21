"""结构化 system prompt section 的构建与渲染测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from mini_pi.agent.prompt import build_sections, build_system_prompt, render_sections
from mini_pi.llm.types import ToolSchema


def tool(name: str, description: str) -> ToolSchema:
    """构造只关注名称与描述的工具 schema。"""
    return ToolSchema(name=name, description=description, parameters={})


def test_build_sections_has_stable_ids_and_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """相同输入生成固定顺序、可重复比较的 section。"""
    monkeypatch.setattr("mini_pi.agent.prompt.platform.system", lambda: "Darwin")
    tools = [tool("read", "Read a file."), tool("edit", "Edit a file.")]

    first = build_sections(cwd=Path("/work/project"), tools=tools)
    second = build_sections(cwd=Path("/work/project"), tools=tools)

    assert [section.id for section in first] == [
        "preamble",
        "environment",
        "rules",
        "tools",
    ]
    assert first == second


def test_empty_tools_section_keeps_legacy_heading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """空工具列表保留 Tools section 与旧 prompt 的尾部空行。"""
    monkeypatch.setattr("mini_pi.agent.prompt.platform.system", lambda: "Darwin")

    sections = build_sections(cwd=Path("/work/project"), tools=[])

    assert sections[-1].id == "tools"
    assert sections[-1].content == ""
    assert render_sections(sections).endswith("# Tools\n\n")


def test_special_characters_are_preserved_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """路径和工具描述属于模型文本，不进行 XML/HTML 转义。"""
    monkeypatch.setattr("mini_pi.agent.prompt.platform.system", lambda: "Darwin")
    sections = build_sections(
        cwd=Path('/work/a<&"b'),
        tools=[tool("inspect", 'Read <tag> & "quoted" content.')],
    )

    rendered = render_sections(sections)

    assert '- Workspace root: /work/a<&"b' in rendered
    assert '- inspect: Read <tag> & "quoted" content.' in rendered
    assert "&lt;" not in rendered


def test_build_system_prompt_remains_byte_compatible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """旧入口改由 sections 渲染后仍返回完全相同的 prompt。"""
    monkeypatch.setattr("mini_pi.agent.prompt.platform.system", lambda: "Darwin")
    tools = [tool("read", "Read a file.")]
    expected = """You are mini-pi, a coding agent working inside a local workspace.

# Environment
- Workspace root: /work/project
- Platform: darwin

# Working rules
- All paths are resolved inside the workspace; paths outside the workspace are rejected.
- Before changing code, locate it with the search tool and read it with the read tool. Never guess file contents.
- Prefer the edit tool for minimal changes; use the write tool only for new files or full rewrites.
- Verify changes with the bash tool (tests/build/lint) and inspect diffs with git_diff.
- Tool errors are returned to you as error observations; read them and adjust instead of repeating the same call.
- When the task is complete, stop calling tools and summarize what changed and how it was verified.

# Tools
- read: Read a file.
"""

    sections = build_sections(cwd=Path("/work/project"), tools=tools)

    assert render_sections(sections) == expected
    assert build_system_prompt(cwd=Path("/work/project"), tools=tools) == expected
