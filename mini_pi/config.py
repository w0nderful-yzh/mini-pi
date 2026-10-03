"""用户级严格权限与本地 server 配置，不读取仓库配置。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from mini_pi.errors import MiniPiError

PermissionMode = Literal["workspace", "readonly"]
EnvName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$", strict=True)]
Text = Annotated[str, StringConstraints(min_length=1, strict=True)]
ToolName = Annotated[str, StringConstraints(min_length=1, max_length=1024, strict=True)]
DEFAULT_CONFIG_PATH = Path.home() / ".mini-pi" / "config.json"


class ServerConfig(BaseModel):
    """显式本地进程配置；环境值只能引用宿主环境变量名。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    kind: Literal["lsp", "mcp"]
    argv: tuple[Text, ...] = Field(min_length=1)
    cwd: Path | None = None
    env: dict[EnvName, EnvName] = Field(default_factory=dict)
    timeout: float = Field(default=10.0, gt=0, le=120)
    tools: tuple[ToolName, ...] = Field(default=(), max_length=32)
    readonly_tools: tuple[ToolName, ...] = Field(default=(), max_length=32)

    @model_validator(mode="after")
    def validate_tools(self) -> Self:
        """MCP 只启用显式白名单，可信只读必须是白名单的子集。"""
        if len(set(self.tools)) != len(self.tools) or len(set(self.readonly_tools)) != len(
            self.readonly_tools
        ):
            raise ValueError("duplicate configured tools")
        if not set(self.readonly_tools) <= set(self.tools):
            raise ValueError("readonly_tools must be a subset of tools")
        if self.kind == "lsp" and (self.tools or self.readonly_tools):
            raise ValueError("LSP tools are fixed by the host")
        return self

    @field_validator("cwd")
    @classmethod
    def absolute_cwd(cls, value: Path | None) -> Path | None:
        """相对目录无法稳定绑定启动授权，必须显式拒绝。"""
        if value is not None and not value.is_absolute():
            raise ValueError("server cwd must be absolute")
        return value

    @field_validator("env")
    @classmethod
    def exclude_provider_keys(cls, value: dict[str, str]) -> dict[str, str]:
        """Provider 密钥不允许作为外部进程环境配置。"""
        if {"OPENAI_API_KEY", "DEEPSEEK_API_KEY"} & (set(value) | set(value.values())):
            raise ValueError("provider API keys cannot be passed to child processes")
        return value


class UserConfig(BaseModel):
    """用户侧权限模式和显式启用的本地 server，未知字段直接拒绝。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    permissions: PermissionMode = "workspace"
    servers: dict[
        Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,64}$")], ServerConfig
    ] = Field(default_factory=dict)

    @model_validator(mode="after")
    def single_lsp(self) -> Self:
        """当前只接一个 LSP server，多语言选择不能靠隐含默认值决定。"""
        if sum(server.kind == "lsp" for server in self.servers.values()) > 1:
            raise ValueError("only one LSP server can be configured")
        if sum(server.kind == "mcp" for server in self.servers.values()) > 4:
            raise ValueError("at most four MCP servers can be configured")
        if sum(len(server.tools) for server in self.servers.values()) > 32:
            raise ValueError("at most 32 MCP tools can be enabled")
        return self


def load_config(path: Path | None = None) -> UserConfig:
    """仅加载用户级文件；缺省关闭所有 server，损坏或未知配置报错。"""
    target = path or DEFAULT_CONFIG_PATH
    if not target.exists():
        return UserConfig()
    try:
        return UserConfig.model_validate_json(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValidationError, json.JSONDecodeError) as exc:
        # 不回显配置正文，避免错误字段携带的秘密进入终端。
        raise MiniPiError(f"invalid user config: {target}") from exc
