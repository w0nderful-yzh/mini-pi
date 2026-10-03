"""有界白名单目录与显式反向身份，不让外部通知改变活动工具集。"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from mini_pi.mcp.client import MCPClient, MCPError
from mini_pi.security import SecurityPolicy, fingerprint
from mini_pi.tools.mcp import MCPTool

CatalogRecorder = Callable[[dict[str, Any]], None]


def tool_alias(server_id: str, original_name: str) -> str:
    """两个 Provider 共用 ASCII 别名，完整身份摘要不靠名称截断反推。"""
    stem = re.sub(r"[^A-Za-z0-9_-]", "_", original_name)[:16]
    digest = fingerprint(json.dumps([server_id, original_name], ensure_ascii=False))[:24]
    return f"mcp_{server_id[:12]}_{stem}_{digest}"


def contains_secret(value: Any, secrets: list[str]) -> bool:
    """schema 与原名若携带凭据就拒绝，不通过脱敏改写参数语义。"""
    if isinstance(value, str):
        return any(secret and secret in value for secret in secrets)
    if isinstance(value, dict):
        return any(
            contains_secret(key, secrets) or contains_secret(item, secrets)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(contains_secret(item, secrets) for item in value)
    return False


class MCPCatalog:
    """用户白名单是唯一启用来源；目录写盘成功后才安装到 Registry。"""

    def __init__(self, security: SecurityPolicy, record: CatalogRecorder | None = None) -> None:
        """注册时保持关闭，目录发现延后至任务前或显式刷新。"""
        self.security = security
        self._record = record
        self.clients: dict[str, MCPClient] = {}
        self.tools: dict[str, MCPTool] = {}
        self._ready = False
        self._fingerprint: str | None = None

    @property
    def ready(self) -> bool:
        """失效连接必须在下次任务前重新发现，通知变更则等待显式 reload。"""
        return self._ready and all(client.connected for client in self.clients.values())

    @property
    def needs_reload(self) -> bool:
        """目录变化只是宿主状态，不能修改当前 run 的工具 schema。"""
        return any(client.changed.is_set() for client in self.clients.values())

    def prepare(self, reserved: set[str]) -> dict[str, MCPTool]:
        """有界发现并原子安装，不支持的白名单工具明确失败而非静默放宽。"""
        if self.ready:
            return self.tools
        self.close()
        clients: dict[str, MCPClient] = {}
        tools: dict[str, MCPTool] = {}
        bindings: list[dict[str, Any]] = []
        try:
            for server_id, config in sorted(self.security.mcp_servers.items()):
                client = MCPClient(self.security, server_id)
                clients[server_id] = client
                discovered = {tool.name: tool for tool in client.list_tools()}
                for name in config.tools:
                    if name not in discovered:
                        raise MCPError(
                            f"configured MCP tool is unavailable: {server_id}/{client.redact(name)}"
                        )
                    tool = discovered[name]
                    if tool.execution is not None and tool.execution.taskSupport == "required":
                        raise MCPError("MCP task-required tools are unsupported")
                    if not tool.name or len(tool.name) > 1024 or len(tool.description or "") > 4096:
                        raise MCPError("MCP name/description exceeds limits")
                    if contains_secret(
                        [server_id, tool.name, tool.inputSchema, tool.outputSchema],
                        self.security.secrets,
                    ):
                        raise MCPError("MCP tool identity/schema contains a configured credential")
                    alias = tool_alias(server_id, tool.name)
                    if (
                        not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", alias)
                        or alias in reserved
                        or alias in tools
                    ):
                        raise MCPError("MCP Provider alias collision or invalid name")
                    try:
                        tools[alias] = MCPTool(
                            client, tool, alias, readonly=name in config.readonly_tools
                        )
                    except MCPError as exc:
                        raise MCPError(f"cannot enable MCP tool {server_id}/{name}: {exc}") from exc
                    bindings.append(
                        {
                            "server_id": server_id,
                            "original_name": name,
                            "alias": alias,
                            "input_schema": tool.inputSchema,
                            "output_schema": tool.outputSchema,
                            "schema_fingerprint": fingerprint(
                                json.dumps([tool.inputSchema, tool.outputSchema], sort_keys=True)
                            ),
                            "config_fingerprint": fingerprint(config.model_dump_json()),
                            "access": tools[alias].access,
                        }
                    )
            if (
                sum(len(json.dumps(item, ensure_ascii=False).encode("utf-8")) for item in bindings)
                > 262_144
            ):
                raise MCPError("MCP enabled catalog exceeds 256 KB")
            identity = fingerprint(json.dumps(bindings, sort_keys=True, ensure_ascii=False))
            if self._record is not None and identity != self._fingerprint:
                self._record({"fingerprint": identity, "tools": bindings})
            self.clients, self.tools = clients, tools
            self._fingerprint = identity
            self._ready = True
            return tools
        except BaseException:
            for client in clients.values():
                client.close()
            raise

    def close(self) -> None:
        """关闭各 server 并标记需重新发现，不恢复一次性启动批准。"""
        for client in self.clients.values():
            client.close()
        self.clients.clear()
        self.tools.clear()
        self._ready = False
