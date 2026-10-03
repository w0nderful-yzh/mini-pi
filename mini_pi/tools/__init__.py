"""工具包出口：默认工具注册表装配。"""

from __future__ import annotations

from mini_pi.interaction import InteractionHandler
from mini_pi.security import SecurityPolicy
from mini_pi.tools.ask_user import AskUserTool
from mini_pi.tools.bash import BashTool
from mini_pi.tools.edit import EditTool
from mini_pi.tools.git import GitDiffTool
from mini_pi.tools.git_status import GitStatusTool
from mini_pi.tools.read import ReadTool
from mini_pi.tools.registry import ToolRegistry
from mini_pi.tools.search import SearchTool
from mini_pi.tools.write import WriteTool
from mini_pi.workspace.workspace import Workspace

__all__ = ["build_default_registry"]


def build_default_registry(
    workspace: Workspace, *, interaction: InteractionHandler | None = None,
    security: SecurityPolicy | None = None,
) -> ToolRegistry:
    """装配默认工具；顺序即 system prompt 与 schema 的展示顺序。"""
    registry = ToolRegistry(security or SecurityPolicy(cwd=workspace.root))
    registry.register(ReadTool(workspace))
    registry.register(WriteTool(workspace))
    registry.register(EditTool(workspace))
    registry.register(SearchTool(workspace))
    registry.register(BashTool(workspace))
    registry.register(GitDiffTool(workspace))
    registry.register(GitStatusTool(workspace))
    registry.register(AskUserTool(interaction))
    registry.configure_lsp(workspace)
    registry.configure_mcp()
    return registry
