"""宿主权限与单次审批：执行前记录决定，不从模型或历史恢复授权。"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from mini_pi.config import ServerConfig, UserConfig
from mini_pi.errors import InteractionUnavailableError, PermissionDeniedError, SessionError
from mini_pi.interaction import ApprovalRequest, ApprovalResponse

ToolAccess = Literal["read", "write", "interaction", "approval"]
ApprovalHandler = Callable[[ApprovalRequest], ApprovalResponse]


@dataclass(frozen=True, slots=True)
class ControlDecision:
    """不包含原始参数或秘密的单次审批审计事实。"""

    target: str
    identity: str
    fingerprint: str
    decision: Literal["allow", "deny"]


ControlRecorder = Callable[[ControlDecision], None]


def fingerprint(text: str) -> str:
    """固定文本指纹只用于绑定和审计，不作为未来授权凭据。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def child_environment() -> dict[str, str]:
    """子进程仅继承执行与编码所需的明确环境，避免父环境凭据透传。"""
    return {
        name: os.environ[name]
        for name in ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "TZ")
        if name in os.environ
    }


class SecurityPolicy:
    """串行权限检查；一次回答只批准当前参数，没有授权缓存。"""

    def __init__(
        self,
        *,
        cwd: Path,
        config: UserConfig | None = None,
        approval: ApprovalHandler | None = None,
        record: ControlRecorder | None = None,
    ) -> None:
        """复制用户配置，保存宿主函数，不接触模型消息或 JSONL。"""
        self.cwd = cwd.resolve()
        self._config = (config or UserConfig()).model_copy(deep=True)
        self._approval = approval
        self._record = record

    def with_recorder(self, record: ControlRecorder) -> SecurityPolicy:
        """为新 Session 独立绑定审计入口，旧运行时的绑定不被替换。"""
        return SecurityPolicy(
            cwd=self.cwd, config=self._config, approval=self._approval, record=record
        )

    @property
    def config(self) -> UserConfig:
        """返回用户配置的独立副本，外部目录不能修改宿主规则。"""
        return self._config.model_copy(deep=True)

    def with_config(self, config: UserConfig) -> SecurityPolicy:
        """显式空闲刷新复用审批与审计函数，但不继承任何批准。"""
        return SecurityPolicy(
            cwd=self.cwd, config=config, approval=self._approval, record=self._record
        )

    @property
    def mcp_servers(self) -> dict[str, ServerConfig]:
        """仅返回带显式工具白名单的本地 MCP 配置。"""
        return {
            name: server.model_copy(deep=True)
            for name, server in self._config.servers.items()
            if server.kind == "mcp" and server.tools
        }

    @property
    def mode(self) -> str:
        """暴露本次宿主权限模式，历史会话不能覆盖它。"""
        return self._config.permissions

    @property
    def lsp_server_id(self) -> str | None:
        """仅返回用户显式启用的 LSP，不从工作区发现或启动服务。"""
        return next((name for name, server in self._config.servers.items() if server.kind == "lsp"), None)

    def server_timeout(self, server_id: str) -> float:
        """读取当前明确配置的协议等待时限。"""
        return self._config.servers[server_id].timeout

    @property
    def secrets(self) -> list[str]:
        """提供已配置环境引用的现存值供 CLI 脱敏，不显示或持久化。"""
        return [
            os.environ[ref]
            for server in self._config.servers.values()
            for ref in server.env.values()
            if os.environ.get(ref)
        ]

    def check_tool(
        self,
        *,
        name: str,
        identity: str,
        access: ToolAccess,
        parameters: str,
        cwd: Path | None = None,
    ) -> None:
        """只读和问询允许，readonly 拒绝其他操作，workspace 对未知与 shell 每次审批。"""
        if access in {"read", "interaction"}:
            return
        if self.mode == "readonly":
            raise PermissionDeniedError(f"{name} is denied by readonly permissions")
        if access == "write":
            return
        self._approve(name, identity, parameters, cwd or self.cwd)

    def _approve(self, target: str, identity: str, parameters: str, cwd: Path) -> None:
        """先取得真实决定并写审计，再允许调用方发生副作用。"""
        if self._approval is None:
            raise InteractionUnavailableError(
                "approval requires an interactive terminal or host approval handler"
            )
        config_text = self._config.model_dump_json()
        binding = fingerprint(json.dumps([target, identity, parameters, str(cwd), config_text]))
        request = ApprovalRequest(target, identity, parameters, str(cwd), binding)
        try:
            response = self._approval(request)
        except EOFError:
            raise KeyboardInterrupt from None
        if response.decision == "cancel":
            raise KeyboardInterrupt
        if self._config.model_dump_json() != config_text:
            raise InteractionUnavailableError(
                "configuration changed during approval; nothing executed"
            )
        decision = ControlDecision(target, identity, binding, response.decision)
        if self._record is not None:
            try:
                self._record(decision)
            except SessionError as exc:
                raise InteractionUnavailableError(
                    "approval audit could not be persisted; nothing executed"
                ) from exc
        if response.decision == "deny":
            raise PermissionDeniedError(f"{target} was denied by the user; nothing executed")

    def start_server(self, server_id: str) -> subprocess.Popen[bytes]:
        """仅对显式配置的本地 argv 启动进程，审批失败不创建子进程。"""
        server = self._config.servers.get(server_id)
        if server is None:
            raise PermissionDeniedError(f"server is not configured: {server_id}")
        cwd = server.cwd or self.cwd
        env = child_environment()
        for name, ref in server.env.items():
            value = os.environ.get(ref)
            if value is None:
                raise InteractionUnavailableError(
                    f"required server environment variable is missing: {ref}"
                )
            env[name] = value
        parameters = json.dumps(
            {
                "argv": server.argv,
                "cwd": str(cwd),
                "env_names": sorted(env),
                "env_fingerprint": fingerprint(json.dumps(env, sort_keys=True)),
            },
            ensure_ascii=False,
        )
        # 配置 argv 是唯一启动来源；不拼 shell，也不采用 server 自报的信任标记。
        self._approve(server_id, f"server:{server.kind}", parameters, cwd)
        return subprocess.Popen(
            server.argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
