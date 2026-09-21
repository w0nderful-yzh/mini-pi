from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from mini_pi.cli.app import app, create_llm
from mini_pi.errors import MiniPiError

runner = CliRunner()


def test_create_llm_rejects_unknown_provider() -> None:
    """未知 provider 直接报参数错误，不做猜测。"""
    import typer

    with pytest.raises(typer.BadParameter, match="unsupported provider"):
        create_llm("ollama", None)


def test_missing_api_key_exits_with_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """缺少 API Key 时以退出码 1 失败，并提示缺失的环境变量。"""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    result = runner.invoke(app, ["hello", "--cwd", str(tmp_path)])
    assert result.exit_code == 1
    assert "OPENAI_API_KEY" in result.output


def test_help_lists_options() -> None:
    """帮助信息包含主要选项。"""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "--provider" in result.output
    assert "--max-steps" in result.output


def test_missing_api_key_raises_minipi_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """DeepSeek 使用独立的 DEEPSEEK_API_KEY。"""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(MiniPiError):
        create_llm("deepseek", None)
