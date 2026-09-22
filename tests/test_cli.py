from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from mini_pi.auth import ConnectionPreference
from mini_pi.cli.app import app, create_llm
from mini_pi.errors import LLMError, MiniPiError
from tests.conftest import FakeLLMClient

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolate_connection_preferences(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """CLI 测试不读取或改写用户真实的认证与 Session 文件。"""
    monkeypatch.setattr("mini_pi.cli.app.load_last_connection", lambda: None)
    monkeypatch.setattr(
        "mini_pi.cli.app.save_last_connection",
        lambda provider, model: Path("/tmp/fake-auth.json"),
    )
    monkeypatch.setattr("mini_pi.session.jsonl._sessions_root", lambda path: tmp_path / "sessions")


def test_create_llm_rejects_unknown_provider() -> None:
    """未知 provider 直接报参数错误，不做猜测。"""
    import typer

    with pytest.raises(typer.BadParameter, match="unsupported provider"):
        create_llm("ollama", None)


def test_missing_api_key_exits_with_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """缺少 API Key 时以退出码 1 失败，并提示 /connect 或环境变量。"""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr("mini_pi.cli.app.resolve_api_key", lambda *args, **kwargs: None)
    result = runner.invoke(app, ["hello", "--cwd", str(tmp_path)])
    assert result.exit_code == 1
    assert "OPENAI_API_KEY" in result.output
    assert "/connect" in result.output


def test_help_lists_options() -> None:
    """帮助信息包含主要选项。"""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "--provider" in result.output
    assert "--max-steps" in result.output
    assert "--no-session" in result.output
    assert "--resume" in result.output
    assert "--continue" in result.output


def test_missing_api_key_raises_minipi_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """DeepSeek 使用独立的 DEEPSEEK_API_KEY。"""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr("mini_pi.cli.app.resolve_api_key", lambda *args, **kwargs: None)
    with pytest.raises(MiniPiError):
        create_llm("deepseek", None)


def test_interactive_without_key_hints_connect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """无 Key 进入交互模式，提示使用 /connect 而不是直接退出。"""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr("mini_pi.cli.app.resolve_api_key", lambda *args, **kwargs: None)
    result = runner.invoke(app, ["--cwd", str(tmp_path)], input="/exit\n")
    assert result.exit_code == 0
    assert "/connect" in result.output


def test_connect_saves_verified_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """验证通过后保存凭据与模型选择，并替换 Agent 的 LLM 客户端。"""
    saved: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        "mini_pi.cli.app.create_llm", lambda provider, model=None, **kwargs: FakeLLMClient([])
    )
    monkeypatch.setattr(
        "mini_pi.cli.app.ask_credentials", lambda console, default: ("deepseek", "sk-test")
    )
    monkeypatch.setattr("mini_pi.cli.app.verify_credentials", lambda provider, key, model: None)
    monkeypatch.setattr(
        "mini_pi.cli.app.save_connection",
        lambda provider, key, model: saved.append((provider, key, model))
        or Path("/tmp/fake-auth.json"),
    )
    result = runner.invoke(app, ["--cwd", str(tmp_path), "--no-session"], input="/connect\n/exit\n")
    assert result.exit_code == 0
    assert saved == [("deepseek", "sk-test", "deepseek-chat")]
    assert "saved to" in result.output


def test_connect_verification_failure_does_not_save(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Key 验证失败时不落盘，并提示失败原因。"""
    saved: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        "mini_pi.cli.app.create_llm", lambda provider, model=None, **kwargs: FakeLLMClient([])
    )
    monkeypatch.setattr(
        "mini_pi.cli.app.ask_credentials", lambda console, default: ("openai", "bad-key")
    )

    def fail_verify(provider: str, key: str, model: str | None) -> None:
        raise LLMError("401 unauthorized", retryable=False)

    monkeypatch.setattr("mini_pi.cli.app.verify_credentials", fail_verify)
    monkeypatch.setattr(
        "mini_pi.cli.app.save_connection",
        lambda provider, key, model: saved.append((provider, key, model))
        or Path("/tmp/fake-auth.json"),
    )
    result = runner.invoke(app, ["--cwd", str(tmp_path), "--no-session"], input="/connect\n/exit\n")
    assert result.exit_code == 0
    assert saved == []
    assert "verification failed" in result.output


def test_startup_restores_last_provider_and_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """无显式参数时使用上次成功保存的 provider/model。"""
    calls: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        "mini_pi.cli.app.load_last_connection",
        lambda: ConnectionPreference(provider="deepseek", model="deepseek-reasoner"),
    )
    monkeypatch.setattr(
        "mini_pi.cli.app.create_llm",
        lambda provider, model=None, **kwargs: calls.append((provider, model))
        or FakeLLMClient([]),
    )

    result = runner.invoke(app, ["--cwd", str(tmp_path)], input="/exit\n")

    assert result.exit_code == 0
    assert calls == [("deepseek", "deepseek-reasoner")]


def test_explicit_selection_overrides_and_updates_last_connection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """显式 CLI 参数优先，并成为后续启动的新默认选择。"""
    calls: list[tuple[str, str | None]] = []
    saved: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "mini_pi.cli.app.load_last_connection",
        lambda: ConnectionPreference(provider="deepseek", model="deepseek-reasoner"),
    )
    monkeypatch.setattr(
        "mini_pi.cli.app.create_llm",
        lambda provider, model=None, **kwargs: calls.append((provider, model))
        or FakeLLMClient([]),
    )
    monkeypatch.setattr(
        "mini_pi.cli.app.save_last_connection",
        lambda provider, model: saved.append((provider, model)) or Path("/tmp/fake-auth.json"),
    )

    result = runner.invoke(
        app,
        [
            "--provider",
            "openai",
            "--model",
            "gpt-custom",
            "--cwd",
            str(tmp_path),
        ],
        input="/exit\n",
    )

    assert result.exit_code == 0
    assert calls == [("openai", "gpt-custom")]
    assert saved == [("openai", "gpt-custom")]


def test_task_without_key_shows_connect_hint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """交互模式下未配置 Key 就输入任务，提示先 /connect。"""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr("mini_pi.cli.app.resolve_api_key", lambda *args, **kwargs: None)
    result = runner.invoke(app, ["--cwd", str(tmp_path)], input="fix bug\n/exit\n")
    assert result.exit_code == 0
    assert "/connect" in result.output


def test_repl_survives_unexpected_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """程序缺陷要可见，但不应该让整个 REPL 退出。"""
    monkeypatch.setattr(
        "mini_pi.cli.app.create_llm", lambda provider, model=None, **kwargs: FakeLLMClient([])
    )
    result = runner.invoke(app, ["--cwd", str(tmp_path)], input="do something\n/exit\n")
    assert result.exit_code == 0
    assert "unexpected error" in result.output
