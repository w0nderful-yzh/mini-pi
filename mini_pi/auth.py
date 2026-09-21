"""API Key 与上次连接配置的安全持久化。"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mini_pi.errors import MiniPiError

# 全局凭据文件；目录 0700、文件 0600
DEFAULT_AUTH_PATH = Path.home() / ".mini-pi" / "auth.json"
_LAST_CONNECTION_KEY = "_last_connection"


@dataclass(frozen=True, slots=True)
class ConnectionPreference:
    """上次使用的 provider/model；不包含 API Key。"""

    provider: str
    model: str


def resolve_api_key(provider: str, *, env_var: str, path: Path | None = None) -> str | None:
    """解析 Key：环境变量 > auth.json > None。"""
    env_value = os.environ.get(env_var)
    if env_value:
        return env_value
    return load_api_key(provider, path=path)


def load_api_key(provider: str, *, path: Path | None = None) -> str | None:
    auth_path = path or DEFAULT_AUTH_PATH
    if not auth_path.is_file():
        return None
    data = _read_auth_file(auth_path)
    entry = data.get(provider)
    if entry is None:
        return None
    if not isinstance(entry, dict) or not isinstance(entry.get("api_key"), str) or not entry["api_key"]:
        raise MiniPiError(f"invalid credential for provider {provider!r} in {auth_path}")
    return entry["api_key"]


def load_last_connection(*, path: Path | None = None) -> ConnectionPreference | None:
    """读取上次使用的 provider/model；未保存时返回 None。"""
    auth_path = path or DEFAULT_AUTH_PATH
    if not auth_path.is_file():
        return None
    data = _read_auth_file(auth_path)
    entry = data.get(_LAST_CONNECTION_KEY)
    if entry is None:
        return None
    if (
        not isinstance(entry, dict)
        or not isinstance(entry.get("provider"), str)
        or not entry["provider"].strip()
        or not isinstance(entry.get("model"), str)
        or not entry["model"].strip()
    ):
        raise MiniPiError(f"invalid last connection in {auth_path}")
    return ConnectionPreference(provider=entry["provider"], model=entry["model"])


def save_api_key(provider: str, api_key: str, *, path: Path | None = None) -> Path:
    """合并写入凭据文件；原子替换，权限收紧到 0600。"""
    if not api_key.strip():
        raise ValueError("api_key must not be empty")
    auth_path = path or DEFAULT_AUTH_PATH
    data: dict[str, Any] = _read_auth_file(auth_path) if auth_path.is_file() else {}
    data[provider] = {"api_key": api_key}
    return _write_auth_file(auth_path, data)


def save_last_connection(
    provider: str, model: str, *, path: Path | None = None
) -> Path:
    """保存默认 provider/model，同时保留已有凭据。"""
    _validate_connection(provider, model)
    auth_path = path or DEFAULT_AUTH_PATH
    data: dict[str, Any] = _read_auth_file(auth_path) if auth_path.is_file() else {}
    data[_LAST_CONNECTION_KEY] = {"provider": provider, "model": model}
    return _write_auth_file(auth_path, data)


def save_connection(
    provider: str,
    api_key: str,
    model: str,
    *,
    path: Path | None = None,
) -> Path:
    """原子保存验证成功的 API Key 及其 provider/model 选择。"""
    if not api_key.strip():
        raise ValueError("api_key must not be empty")
    _validate_connection(provider, model)
    auth_path = path or DEFAULT_AUTH_PATH
    data: dict[str, Any] = _read_auth_file(auth_path) if auth_path.is_file() else {}
    data[provider] = {"api_key": api_key}
    data[_LAST_CONNECTION_KEY] = {"provider": provider, "model": model}
    return _write_auth_file(auth_path, data)


def _validate_connection(provider: str, model: str) -> None:
    """provider/model 是启动配置，空值属于调用方错误。"""
    if not provider.strip():
        raise ValueError("provider must not be empty")
    if not model.strip():
        raise ValueError("model must not be empty")


def _write_auth_file(auth_path: Path, data: dict[str, Any]) -> Path:
    """以 0600 权限原子写入完整认证配置。"""
    auth_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # mkstemp 默认 0600；写入同目录临时文件后原子替换
    fd, tmp_name = tempfile.mkstemp(dir=auth_path.parent, prefix=".auth.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, auth_path)
    except BaseException:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise
    return auth_path


def _read_auth_file(auth_path: Path) -> dict[str, Any]:
    """读取并校验 auth.json；损坏时显式报错，不静默重置。"""
    try:
        raw = json.loads(auth_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MiniPiError(f"failed to read auth file {auth_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise MiniPiError(f"invalid auth file {auth_path}: expected a JSON object")
    return raw
