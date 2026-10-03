"""只读 LSP 引用查询，复用同一连接和严格位置参数。"""

from __future__ import annotations

from mini_pi.lsp.client import LspClient
from mini_pi.tools.base import Tool, ToolResult
from mini_pi.tools.lsp_definition import PositionArgs


class LspReferencesTool(Tool):
    """查询引用和声明，不替换 search 或触发自动重构。"""

    name = "lsp_references"
    description = "Find Python symbol references, including declarations, using local LSP. Positions are 1-based Unicode characters; results are bounded and external targets are restricted."
    args_model = PositionArgs
    access = "read"

    def __init__(self, client: LspClient) -> None:
        """绑定当前运行时唯一的长驻连接。"""
        self._client = client

    def execute(self, path: str, line: int, column: int) -> ToolResult:
        """返回有界真实引用位置，查询方法由宿主固定。"""
        return ToolResult(
            content=self._client.locations("textDocument/references", path, line, column)
        )
