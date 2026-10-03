"""只读 LSP 诊断，只报告已确认对应当前内容的结果。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from mini_pi.lsp.client import LspClient
from mini_pi.tools.base import Tool, ToolResult


class DiagnosticsArgs(BaseModel):
    """显式查询一个文件，不支持全仓库诊断扫描。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    path: str = Field(min_length=1)


class LspDiagnosticsTool(Tool):
    """返回文件版本、内容指纹和新鲜诊断，未就绪时明确失败。"""

    name = "lsp_diagnostics"
    description = "Get current Python file diagnostics using local LSP after synchronizing open files with disk. Reports version/content_hash/freshness; unversioned or stale push diagnostics are errors, not zero errors. Does not replace tests."
    args_model = DiagnosticsArgs
    access = "read"

    def __init__(self, client: LspClient) -> None:
        """绑定当前运行时唯一的长驻连接。"""
        self._client = client

    def execute(self, path: str) -> ToolResult:
        """返回当前诊断或明确错误，不把客户端默认值当作零诊断。"""
        return ToolResult(content=self._client.diagnostics(path))
