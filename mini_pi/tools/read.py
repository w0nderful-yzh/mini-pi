"""read 工具：分页读取 workspace 内的 UTF-8 文本文件。"""

from __future__ import annotations

from pydantic import BaseModel, Field

from mini_pi.errors import ToolError
from mini_pi.tools.base import Tool, ToolResult
from mini_pi.tools.truncate import truncate_text
from mini_pi.workspace.workspace import Workspace


class ReadArgs(BaseModel):
    path: str = Field(description="File path relative to the workspace root.")
    offset: int = Field(default=1, ge=1, description="1-based line number to start reading from.")
    limit: int | None = Field(default=None, ge=1, description="Maximum number of lines to read.")


class ReadTool(Tool):
    name = "read"
    description = "Read a UTF-8 text file inside the workspace. Supports offset/limit paging and reports a continuation hint when truncated."
    args_model = ReadArgs
    max_lines = 2000
    max_bytes = 50_000

    def __init__(self, workspace: Workspace) -> None:
        """持有 Workspace，只读不声明 modified_files。"""
        self._workspace = workspace

    def execute(self, path: str, offset: int = 1, limit: int | None = None) -> ToolResult:
        """分页读取 UTF-8 文本；拦二进制/非 UTF-8/越界 offset，截断时附续读提示。"""
        rel = self._workspace.relative(path)
        resolved = self._workspace.resolve(path)
        if not resolved.is_file():
            raise ToolError(f"not a file: {rel}")
        data = self._workspace.read_bytes(path)
        # NUL 字节按二进制处理，避免把乱码当文本喂给模型
        if b"\x00" in data[:8192]:
            raise ToolError(f"binary file is not supported: {rel}")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ToolError(f"file is not valid UTF-8: {rel} ({exc})") from exc
        lines = text.splitlines()
        total = len(lines)
        if offset > total:
            raise ToolError(f"offset {offset} is beyond end of file ({total} lines)")
        window = lines[offset - 1 : offset - 1 + (limit or self.max_lines)]
        body, truncated = truncate_text(
            "\n".join(window), max_lines=self.max_lines, max_bytes=self.max_bytes
        )
        if body == "" and window:
            # 单行超过字节上限时给出替代方案，而不是返回空内容
            return ToolResult(
                content=f"Selected line is too long to display (byte cap {self.max_bytes}). Use bash with sed or awk to inspect it.",
                details={"path": rel, "total_lines": total},
            )
        shown = body.count("\n") + 1 if body else 0
        last = offset + shown - 1
        if truncated or last < total:
            body += (
                f"\n[Showing lines {offset}-{last} of {total}. "
                f"Use offset={last + 1} to continue.]"
            )
        # 只读工具：details 仅用于展示，不声明 modified_files
        return ToolResult(content=body, details={"path": rel, "total_lines": total})
