"""write 工具：原子写整文件。"""

from __future__ import annotations

from pydantic import BaseModel, Field

from mini_pi.tools.base import Tool, ToolResult
from mini_pi.workspace.workspace import Workspace


class WriteArgs(BaseModel):
    path: str = Field(description="File path relative to the workspace root.")
    content: str = Field(description="Full file content to write.")


class WriteTool(Tool):
    name = "write"
    description = "Create or fully rewrite a file inside the workspace. Parent directories are created automatically and writes are atomic."
    args_model = WriteArgs

    def __init__(self, workspace: Workspace) -> None:
        self._workspace = workspace

    def execute(self, path: str, content: str) -> ToolResult:
        rel = self._workspace.relative(path)
        self._workspace.write_text(path, content)
        size = len(content.encode("utf-8"))
        # 显式声明改动文件，供 Agent 追踪 modified_files
        return ToolResult(
            content=f"Wrote {size} bytes to {rel}.",
            details={"path": rel},
            modified_files=[rel],
        )
