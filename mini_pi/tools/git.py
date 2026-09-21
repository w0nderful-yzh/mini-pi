"""git_diff 工具：查看工作区未提交改动。"""

from __future__ import annotations

from pydantic import BaseModel, Field

from mini_pi.errors import ToolError
from mini_pi.tools.base import Tool, ToolResult
from mini_pi.tools.process import run_process
from mini_pi.tools.truncate import truncate_text
from mini_pi.workspace.workspace import Workspace


class GitDiffArgs(BaseModel):
    path: str | None = Field(default=None, description="Limit the diff to this path inside the workspace.")
    staged: bool = Field(default=False, description="Show staged changes (git diff --cached).")


class GitDiffTool(Tool):
    name = "git_diff"
    description = "Show uncommitted changes in the workspace git repository as a unified diff."
    args_model = GitDiffArgs
    max_lines = 2000
    max_bytes = 50_000

    def __init__(self, workspace: Workspace) -> None:
        self._workspace = workspace

    def execute(self, path: str | None = None, staged: bool = False) -> ToolResult:
        argv = ["git", "diff", "--no-color"]
        if staged:
            argv.append("--cached")
        if path is not None:
            # 先经 Workspace 校验，避免把越界路径交给 git
            argv.extend(["--", self._workspace.relative(path)])
        # diff 从开头读起，超过上限时按 head 保留
        result = run_process(argv, cwd=self._workspace.root, timeout_s=30, keep="head")
        if result.exit_code != 0:
            raise ToolError(
                f"git diff failed (exit {result.exit_code}): {result.stderr.strip()}"
            )
        body, truncated = truncate_text(
            result.stdout, max_lines=self.max_lines, max_bytes=self.max_bytes
        )
        if not body.strip():
            body = "No changes."
        elif truncated or result.stdout_truncated:
            body += "\n[output truncated]"
        return ToolResult(content=body, details={"staged": staged, "path": path})
