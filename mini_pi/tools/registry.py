"""Tool 注册、schema 校验与调度。"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from mini_pi.errors import ToolArgumentError, ToolNotFoundError
from mini_pi.llm.types import ToolCall, ToolSchema
from mini_pi.security import ControlRecorder, SecurityPolicy
from mini_pi.tools.base import Tool, ToolResult
from mini_pi.workspace.workspace import Workspace

if TYPE_CHECKING:
    from mini_pi.lsp.client import LspClient


class ToolRegistry:
    """维护 name -> Tool 映射，统一校验参数后再调用工具。"""

    def __init__(self, security: SecurityPolicy | None = None) -> None:
        """初始化空的 name -> Tool 映射。"""
        self._tools: dict[str, Tool] = {}
        self.security = security or SecurityPolicy(cwd=Path.cwd())
        self._lsp: LspClient | None = None

    def configure_lsp(self, workspace: Workspace) -> None:
        """仅在明确配置时注册只读工具，不提前启动或读取任何代码。"""
        from mini_pi.lsp.client import LspClient
        from mini_pi.tools.lsp_definition import LspDefinitionTool
        from mini_pi.tools.lsp_diagnostics import LspDiagnosticsTool
        from mini_pi.tools.lsp_references import LspReferencesTool

        server_id = self.security.lsp_server_id
        if server_id is None:
            return
        if self._lsp is not None:
            raise ValueError("LSP already configured")
        self._lsp = LspClient(workspace, self.security, server_id)
        for tool in (LspDefinitionTool(self._lsp), LspReferencesTool(self._lsp), LspDiagnosticsTool(self._lsp)):
            self.register(tool)

    def close(self) -> None:
        """释放注册表持有的外部连接，后续显式调用会重新审批启动。"""
        if self._lsp is not None:
            self._lsp.close()

    def for_session(self, record: ControlRecorder) -> ToolRegistry:
        """复制注册表并绑定独立审计回调，权限和工具不从 Session 重建。"""
        registry = ToolRegistry(self.security.with_recorder(record))
        registry._tools = self._tools.copy()
        if self._lsp is not None:
            for name in ("lsp_definition", "lsp_references", "lsp_diagnostics"):
                del registry._tools[name]
            registry.configure_lsp(self._lsp.workspace)
        return registry

    def register(self, tool: Tool) -> None:
        """重名注册属于编程错误，直接失败。"""
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool name: {tool.name}")
        self._tools[tool.name] = tool

    def schemas(self) -> list[ToolSchema]:
        """导出工具 schema，供 LLM 请求使用。"""
        return [tool.schema() for tool in self._tools.values()]

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        """校验工具存在与参数后执行；未知工具/参数非法转对应 ToolError。"""
        tool = self._tools.get(name)
        if tool is None:
            raise ToolNotFoundError(f"unknown tool: {name!r}")
        try:
            validated = tool.args_model.model_validate(arguments).model_copy(deep=True)
        except ValidationError as exc:
            # 参数校验失败是可预期失败：转 ToolArgumentError，由 Loop 回传模型纠正
            raise ToolArgumentError(f"invalid arguments for {name!r}: {exc}") from exc
        # 审批只收到不可变文本，执行使用自己的深拷贝，调用方不能在等待时替换参数。
        self.security.check_tool(
            name=name, identity=f"{type(tool).__module__}.{type(tool).__qualname__}",
            access=tool.access, parameters=validated.model_dump_json(), cwd=tool.execution_cwd,
        )
        # 浅取字段：model_dump() 会递归把嵌套模型转成 dict，破坏 execute 的类型标注
        return tool.execute(**dict(validated))

    def validate_batch(self, calls: list[ToolCall]) -> None:
        """独占工具不能混入其他调用，必须在整批任何执行前拒绝。"""
        if len(calls) > 1:
            for call in calls:
                tool = self._tools.get(call.name)
                if tool is not None and tool.exclusive_batch:
                    raise ToolArgumentError(
                        f"{call.name} must be the only tool call in its batch; "
                        "no calls in this batch were executed."
                    )
