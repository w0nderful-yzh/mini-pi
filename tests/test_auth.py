from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from mini_pi.auth import load_api_key, resolve_api_key, save_api_key
from mini_pi.errors import MiniPiError


@pytest.fixture
def auth_path(tmp_path: Path) -> Path:
    return tmp_path / "config" / "auth.json"


def test_env_var_takes_priority(auth_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """环境变量优先于 auth.json，方便 CI / 临时覆盖。"""
    save_api_key("openai", "from-file", path=auth_path)
    monkeypatch.setenv("OPENAI_API_KEY", "from-env")
    assert resolve_api_key("openai", env_var="OPENAI_API_KEY", path=auth_path) == "from-env"


def test_resolve_falls_back_to_file(auth_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """环境变量缺失时读取 auth.json。"""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    save_api_key("openai", "from-file", path=auth_path)
    assert resolve_api_key("openai", env_var="OPENAI_API_KEY", path=auth_path) == "from-file"


def test_resolve_returns_none_when_nothing_configured(
    auth_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert resolve_api_key("openai", env_var="OPENAI_API_KEY", path=auth_path) is None


def test_save_and_load_roundtrip(auth_path: Path) -> None:
    save_api_key("deepseek", "sk-test", path=auth_path)
    assert load_api_key("deepseek", path=auth_path) == "sk-test"


def test_save_merges_existing_providers(auth_path: Path) -> None:
    """新增 provider 不应覆盖已有条目。"""
    save_api_key("openai", "sk-openai", path=auth_path)
    save_api_key("deepseek", "sk-deepseek", path=auth_path)
    data = json.loads(auth_path.read_text(encoding="utf-8"))
    assert data["openai"]["api_key"] == "sk-openai"
    assert data["deepseek"]["api_key"] == "sk-deepseek"


@pytest.mark.skipif(os.name != "posix", reason="POSIX 权限位")
def test_permissions_are_restricted(auth_path: Path) -> None:
    """目录 0700、文件 0600，避免其他用户读取 Key。"""
    save_api_key("openai", "sk-test", path=auth_path)
    assert auth_path.stat().st_mode & 0o777 == 0o600
    assert auth_path.parent.stat().st_mode & 0o777 == 0o700


def test_corrupt_file_is_error(auth_path: Path) -> None:
    """auth.json 损坏时显式报错，不静默覆盖。"""
    auth_path.parent.mkdir(parents=True)
    auth_path.write_text("{not json", encoding="utf-8")
    with pytest.raises(MiniPiError, match="failed to read auth file"):
        load_api_key("openai", path=auth_path)


def test_invalid_entry_is_error(auth_path: Path) -> None:
    auth_path.parent.mkdir(parents=True)
    auth_path.write_text('{"openai": {"api_key": 123}}', encoding="utf-8")
    with pytest.raises(MiniPiError, match="invalid credential"):
        load_api_key("openai", path=auth_path)


def test_empty_key_is_rejected(auth_path: Path) -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        save_api_key("openai", "   ", path=auth_path)
