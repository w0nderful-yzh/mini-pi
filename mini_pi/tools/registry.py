"""Tool 注册、schema 校验与调度。"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic_core import to_json

from mini_pi.config import UserConfig
from mini_pi.errors import ToolArgumentError, ToolNotFoundError
from mini_pi.llm.types import ToolCall, ToolSchema
from mini_pi.security import ControlRecorder, SecurityPolicy
from mini_pi.tools.base import Tool, ToolResult
from mini_pi.workspace.workspace import Workspace

if TYPE_CHECKING:
    from mini_pi.lsp.client import LspClient
    from mini_pi.mcp.catalog import CatalogRecorder, MCPCatalog


class ToolRegistry:
    """维护 name -> Tool 映射，统一校验参数后再调用工具。"""

    def __init__(self, security: SecurityPolicy | None = None) -> None:
        """初始化空的 name -> Tool 映射。"""
        self._tools: dict[str, Tool] = {}
        self.security = security or SecurityPolicy(cwd=Path.cwd())
        self._lsp: LspClient | None = None
        self._mcp: MCPCatalog | None = None
        self._catalog_record: CatalogRecorder | None = None
        self._running = False

    def configure_mcp(self, record: CatalogRecorder | None = None) -> None:
        """仅装配明确的 MCP 配置，Session 在发现前绑定目录审计回调。"""
        # tools 包装配和 MCPTool 相互引用，类型检查之外延后至模块加载完成。
        from mini_pi.mcp.catalog import MCPCatalog

        self._catalog_record = record
        if self.security.mcp_servers:
            self._mcp = MCPCatalog(self.security, record)

    def prepare_mcp(self) -> None:
        """任务前发现目录，失败时移除所有旧外部工具以免误用失效 schema。"""
        if self._running:
            raise ValueError("MCP catalog is frozen during a run")
        if self._mcp is None or self._mcp.ready:
            return
        from mini_pi.tools.mcp import MCPTool

        self._tools = {
            name: tool for name, tool in self._tools.items() if not isinstance(tool, MCPTool)
        }
        self._tools.update(self._mcp.prepare(set(self._tools)))

    def begin_run(self) -> None:
        """冻结整个任务的目录，运行中不响应外部刷新请求。"""
        if self._running:
            raise ValueError("runtime is already running")
        self.prepare_mcp()
        self._running = True

    def end_run(self) -> None:
        """任务退出后解冻宿主刷新入口。"""
        self._running = False

    def reload_mcp(self, config: UserConfig) -> None:
        """仅空闲时刷新 MCP 配置，权限模式与 LSP 仍由当前运行时固定。"""
        from mini_pi.mcp.catalog import MCPCatalog

        if self._running:
            raise ValueError("MCP reload is only allowed while idle")
        current = self.security.config
        servers = {name: server for name, server in current.servers.items() if server.kind != "mcp"}
        for name, server in config.servers.items():
            if server.kind == "mcp":
                if name in servers:
                    raise ValueError("MCP server id conflicts with an existing LSP server")
                servers[name] = server
        replacement = UserConfig(permissions=current.permissions, servers=servers)
        record = self._catalog_record
        if self._mcp is not None:
            self._mcp.close()
        self.security = self.security.with_config(replacement)
        self._mcp = MCPCatalog(self.security, record)
        self.prepare_mcp()

    @property
    def mcp_status(self) -> str:
        """CLI 显示连接来源状态，不把刷新提示注入模型消息。"""
        if self._mcp is None or not self.security.mcp_servers:
            return "MCP disabled"
        state = (
            "needs /mcp reload"
            if self._mcp.needs_reload
            else "ready"
            if self._mcp.ready
            else "not initialized"
        )
        return f"MCP {state}: {', '.join(self.security.mcp_servers)}"

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
        for tool in (
            LspDefinitionTool(self._lsp),
            LspReferencesTool(self._lsp),
            LspDiagnosticsTool(self._lsp),
        ):
            self.register(tool)

    def close(self) -> None:
        """释放注册表持有的外部连接，后续显式调用会重新审批启动。"""
        if self._lsp is not None:
            self._lsp.close()
        if self._mcp is not None:
            self._mcp.close()

    def for_session(
        self, record: ControlRecorder, catalog_record: CatalogRecorder | None = None
    ) -> ToolRegistry:
        """复制注册表并绑定独立审计回调，权限和工具不从 Session 重建。"""
        registry = ToolRegistry(self.security.with_recorder(record))
        from mini_pi.tools.mcp import MCPTool

        registry._tools = {
            name: tool for name, tool in self._tools.items() if not isinstance(tool, MCPTool)
        }
        if self._lsp is not None:
            for name in ("lsp_definition", "lsp_references", "lsp_diagnostics"):
                del registry._tools[name]
            registry.configure_lsp(self._lsp.workspace)
        registry.configure_mcp(catalog_record)
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
        validated = tool.validate_arguments(arguments)
        # 审批只收到不可变文本，执行使用自己的深拷贝，调用方不能在等待时替换参数。
        self.security.check_tool(
            name=name,
            identity=tool.identity,
            access=tool.access,
            parameters=to_json(validated).decode("utf-8"),
            cwd=tool.execution_cwd,
        )
        # 浅取字段：model_dump() 会递归把嵌套模型转成 dict，破坏 execute 的类型标注
        return tool.execute(**validated)

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
