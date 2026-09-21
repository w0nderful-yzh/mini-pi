"""工具包出口：默认工具注册表装配。"""

from __future__ import annotations

from mini_pi.tools.bash import BashTool
from mini_pi.tools.edit import EditTool
from mini_pi.tools.git import GitDiffTool
from mini_pi.tools.read import ReadTool
from mini_pi.tools.registry import ToolRegistry
from mini_pi.tools.search import SearchTool
from mini_pi.tools.write import WriteTool
from mini_pi.workspace.workspace import Workspace

__all__ = ["build_default_registry"]


def build_default_registry(workspace: Workspace) -> ToolRegistry:
    """装配第一阶段六个工具；顺序即 system prompt 与 schema 的展示顺序。"""
    registry = ToolRegistry()
    registry.register(ReadTool(workspace))
    registry.register(WriteTool(workspace))
    registry.register(EditTool(workspace))
    registry.register(SearchTool(workspace))
    registry.register(BashTool(workspace))
    registry.register(GitDiffTool(workspace))
    return registry
