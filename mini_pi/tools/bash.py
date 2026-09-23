"""bash 工具：在 workspace root 执行 shell 命令。"""

from __future__ import annotations

from pydantic import BaseModel, Field

from mini_pi.tools.base import Tool, ToolResult
from mini_pi.tools.process import run_shell
from mini_pi.tools.truncate import truncate_text
from mini_pi.workspace.workspace import Workspace


class BashArgs(BaseModel):
    command: str = Field(description="Shell command to run with cwd set to the workspace root.")
    timeout: int = Field(default=120, ge=1, le=600, description="Timeout in seconds.")


class BashTool(Tool):
    name = "bash"
    description = "Run a shell command in the workspace root. Returns exit code, stdout and stderr. Non-zero exit codes are reported, not raised."
    args_model = BashArgs
    max_lines = 2000
    max_bytes = 50_000

    def __init__(self, workspace: Workspace) -> None:
        """持有 Workspace，命令 cwd 固定为 root。"""
        self._workspace = workspace

    def execute(self, command: str, timeout: int = 120) -> ToolResult:
        """执行命令并截断输出；非零退出码如实回传为 Observation，不抛错。"""
        result = run_shell(command, cwd=self._workspace.root, timeout_s=timeout)
        # 命令输出尾部信息量更大（报错在最后），按 tail 保留
        stdout, stdout_truncated = truncate_text(
            result.stdout, max_lines=self.max_lines, max_bytes=self.max_bytes, keep="tail"
        )
        stderr, stderr_truncated = truncate_text(
            result.stderr, max_lines=self.max_lines, max_bytes=self.max_bytes, keep="tail"
        )
        header = f"exit_code: {result.exit_code}"
        if result.timed_out:
            header += f" (timed out after {timeout}s, process group killed)"
        parts = [
            header,
            "stdout:",
            stdout if stdout else "(empty)",
            "stderr:",
            stderr if stderr else "(empty)",
        ]
        if stdout_truncated or stderr_truncated:
            parts.append("[output truncated]")
        # 不声明 modified_files：shell 命令改了哪些文件无法可靠推断
        return ToolResult(
            content="\n".join(parts),
            details={
                "exit_code": result.exit_code,
                "timed_out": result.timed_out,
                "stdout_truncated": stdout_truncated or result.stdout_truncated,
                "stderr_truncated": stderr_truncated or result.stderr_truncated,
            },
        )
