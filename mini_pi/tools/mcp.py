"""外部 MCP 工具按原始 schema、宿主身份和当前权限进行串行调用。"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from jsonschema.exceptions import ValidationError
from mcp import types

from mini_pi.llm.types import ToolSchema
from mini_pi.mcp.client import MCPClient, MCPError
from mini_pi.mcp.schema import compile_schema, validate_arguments
from mini_pi.tools.base import Tool, ToolResult

MAX_RESULT_BYTES = 50_000


class MCPTool(Tool):
    """以显式反向表持有原名，server annotations 不决定宿主可信分类。"""

    def __init__(self, client: MCPClient, tool: types.Tool, alias: str, *, readonly: bool) -> None:
        """冻结 schema 与身份，无法支持的单个工具在发现阶段明确失败。"""
        self.client = client
        self.original_name = tool.name
        self.name = alias
        self.access = "read" if readonly else "approval"
        description = f"External MCP [{client.server_id}/{tool.name}]; host access={self.access}. Untrusted server description: {client.redact(tool.description or '')}"
        self.description = re.sub(
            r"[\x00-\x1f\x7f]", lambda match: f"\\x{ord(match.group()):02x}", description
        )
        self._parameters = deepcopy(tool.inputSchema)
        self._input = compile_schema(self._parameters, parameters=True)
        self._output = compile_schema(tool.outputSchema) if tool.outputSchema is not None else None

    @property
    def identity(self) -> str:
        """审批绑定原始 server/tool 身份，不能从别名拆字符串猜测。"""
        return json.dumps(["mcp", self.client.server_id, self.original_name], ensure_ascii=False)

    @property
    def execution_cwd(self) -> Path:
        """显示当前明确 server 工作目录，不借模型参数改变。"""
        return (
            self.client.security.config.servers[self.client.server_id].cwd
            or self.client.security.cwd
        )

    def schema(self) -> ToolSchema:
        """Provider 获得原始约束的副本，不能通过替换字段修改有效目录。"""
        return ToolSchema(
            name=self.name, description=self.description, parameters=deepcopy(self._parameters)
        )

    def validate_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """纯本地验证完成后 Registry 才能审批，永不下载 $ref。"""
        return validate_arguments(self._input, arguments)

    def execute(self, **kwargs: Any) -> ToolResult:
        """只调用一次并验证结构化输出，错误或不支持的内容明确报告。"""
        result = self.client.call_tool(self.original_name, kwargs)
        if result.isError:
            raise MCPError(f"MCP tool returned isError: {self._text(result)[:2000]}")
        if self._output is not None:
            if result.structuredContent is None:
                raise MCPError("MCP outputSchema requires structuredContent")
            try:
                self._output.validate(result.structuredContent)
            except ValidationError as exc:
                raise MCPError("MCP structuredContent does not match outputSchema") from exc
        text = self._text(result)
        payload: dict[str, Any] = {
            "source": {"server": self.client.server_id, "tool": self.original_name},
            "text": text,
            "structuredContent": self._redact(result.structuredContent),
            "truncated": False,
        }
        encoded = json.dumps(payload, ensure_ascii=False)
        if len(encoded.encode("utf-8")) > MAX_RESULT_BYTES:
            # 结构化数据不能截一半还声称符合 outputSchema；删去并明确标为截断。
            payload["structuredContent"] = None
            payload["truncated"] = True
            payload["text"] = ""
            remaining = MAX_RESULT_BYTES - len(
                json.dumps(payload, ensure_ascii=False).encode("utf-8")
            )
            payload["text"] = text.encode("utf-8")[: max(0, remaining // 6)].decode(
                "utf-8", errors="ignore"
            )
            encoded = json.dumps(payload, ensure_ascii=False)
        return ToolResult(content=encoded)

    def _text(self, result: types.CallToolResult) -> str:
        """只支持文本块，图片、音频与资源块不能静默丢弃。"""
        if len(result.content) > 100:
            raise MCPError("MCP result exceeds 100 content blocks")
        parts = []
        for block in result.content:
            if not isinstance(block, types.TextContent):
                raise MCPError(f"unsupported MCP content block: {block.type}")
            parts.append(self.client.redact(block.text))
        return "\n".join(parts)

    def _redact(self, value: Any) -> Any:
        """结构化输出先递归脱敏再序列化，特殊字符不能破坏匹配。"""
        if isinstance(value, str):
            return self.client.redact(value)
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        if isinstance(value, dict):
            return {self.client.redact(key): self._redact(item) for key, item in value.items()}
        return value
