"""只读 LSP 定义查询，模型使用一基 Unicode 字符位置。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from mini_pi.lsp.client import LspClient
from mini_pi.tools.base import Tool, ToolResult


class PositionArgs(BaseModel):
    """显式文件与字符位置，不允许模型选择协议方法或 server。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    path: str = Field(min_length=1)
    line: int = Field(ge=1)
    column: int = Field(ge=1)


class LspDefinitionTool(Tool):
    """查询定义位置，不执行 server 提供的编辑或命令。"""

    name = "lsp_definition"
    description = "Find Python symbol definitions with the configured local LSP. path, line and column use 1-based Unicode characters. Startup requires approval; outside-workspace targets are restricted."
    args_model = PositionArgs
    access = "read"

    def __init__(self, client: LspClient) -> None:
        """绑定当前运行时唯一的长驻连接。"""
        self._client = client

    def execute(self, path: str, line: int, column: int) -> ToolResult:
        """查询真实定义并返回有界 JSON，不声明文件改动。"""
        return ToolResult(
            content=self._client.locations("textDocument/definition", path, line, column)
        )
