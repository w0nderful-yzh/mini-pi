from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from mini_pi.auth import ConnectionPreference
from mini_pi.cli.app import _force_utf8, app, create_llm
from mini_pi.errors import LLMError, MiniPiError
from tests.conftest import FakeLLMClient, assistant

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolate_connection_preferences(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """CLI 测试不读取或改写用户真实的认证与 Session 文件。"""
    monkeypatch.setattr("mini_pi.cli.app.load_last_connection", lambda: None)
    monkeypatch.setattr("mini_pi.cli.app.resolve_api_key", lambda *args, **kwargs: None)
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


def test_model_switch_prompts_and_saves_new_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """缺 Key 时 /model 隐藏输入并验证，通过后保存到 auth。"""
    saved: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        "mini_pi.cli.app.create_llm", lambda provider, model=None, **kwargs: FakeLLMClient([])
    )
    monkeypatch.setattr("mini_pi.cli.app.ask_api_key", lambda console, provider: "sk-test")
    monkeypatch.setattr("mini_pi.cli.app.verify_credentials", lambda provider, key, model: None)
    monkeypatch.setattr(
        "mini_pi.cli.app.save_connection",
        lambda provider, key, model: saved.append((provider, key, model))
        or Path("/tmp/fake-auth.json"),
    )
    result = runner.invoke(
        app,
        ["--cwd", str(tmp_path), "--no-session", "--no-banner"],
        input="/model deepseek deepseek-flash\n/exit\n",
    )
    assert result.exit_code == 0, result.output
    assert saved == [("deepseek", "sk-test", "deepseek-flash")]
    assert "model: deepseek/deepseek-flash" in result.output


def test_model_switch_verification_failure_does_not_save(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Key 验证失败时不落盘，并提示失败原因。"""
    saved: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        "mini_pi.cli.app.create_llm", lambda provider, model=None, **kwargs: FakeLLMClient([])
    )
    monkeypatch.setattr("mini_pi.cli.app.ask_api_key", lambda console, provider: "bad-key")

    def fail_verify(provider: str, key: str, model: str | None) -> None:
        raise LLMError("401 unauthorized", retryable=False)

    monkeypatch.setattr("mini_pi.cli.app.verify_credentials", fail_verify)
    monkeypatch.setattr(
        "mini_pi.cli.app.save_connection",
        lambda provider, key, model: saved.append((provider, key, model))
        or Path("/tmp/fake-auth.json"),
    )
    result = runner.invoke(
        app,
        ["--cwd", str(tmp_path), "--no-session", "--no-banner"],
        input="/model openai gpt-5.6-terra\n/exit\n",
    )
    assert result.exit_code == 0, result.output
    assert saved == []
    assert "model switch failed" in result.output


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


def test_force_utf8_overrides_locale_encoding() -> None:
    """stdio 编码不应由 locale 决定：ascii 流重配后仍可写中文。"""
    import io

    stream = io.TextIOWrapper(io.BytesIO(), encoding="ascii", errors="strict")
    _force_utf8(stream, errors="strict")
    assert stream.encoding.lower() == "utf-8"
    stream.write("你好")
    stream.flush()


def test_repl_reports_invalid_input_encoding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """输入字节不是 UTF-8 时给出可执行提示，而不是抛序列化异常。"""
    monkeypatch.setattr("mini_pi.cli.app.resolve_api_key", lambda *args, **kwargs: None)
    calls = {"count": 0}

    def fake_input(prompt: str = "") -> str:
        calls["count"] += 1
        if calls["count"] == 1:
            raise UnicodeDecodeError("utf-8", b"\xe5", 0, 1, "invalid start byte")
        raise EOFError

    monkeypatch.setattr("builtins.input", fake_input)
    result = runner.invoke(app, ["--cwd", str(tmp_path)])
    assert result.exit_code == 0
    assert "invalid input" in result.output
    assert calls["count"] == 2


def test_one_shot_rejects_surrogate_task(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """argv 中的非法 UTF-8 文本以退出码 1 报错，而不是抛序列化异常。"""
    monkeypatch.setattr("mini_pi.cli.app.resolve_api_key", lambda *args, **kwargs: "sk-test")
    monkeypatch.setattr(
        "mini_pi.cli.app.create_llm",
        lambda provider, model=None, **kwargs: FakeLLMClient([assistant("ok")]),
    )
    surrogate_task = b"\xe5".decode("utf-8", "surrogateescape")
    result = runner.invoke(
        app, ["--provider", "openai", surrogate_task, "--cwd", str(tmp_path)]
    )
    assert result.exit_code == 1
    assert "surrogate" in result.output


def test_help_lists_available_commands(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """交互模式 /help 列出可用命令。"""
    monkeypatch.setattr("mini_pi.cli.app.resolve_api_key", lambda *args, **kwargs: None)

    result = runner.invoke(
        app, ["--cwd", str(tmp_path), "--no-banner"], input="/help\n/exit\n"
    )

    assert result.exit_code == 0
    assert "/connect" in result.output
    assert "/help" in result.output
    assert "/exit" in result.output


def test_unknown_command_is_not_sent_to_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """未知 /命令只给提示，不应进入模型调用。"""
    llm = FakeLLMClient([])
    monkeypatch.setattr("mini_pi.cli.app.resolve_api_key", lambda *args, **kwargs: "sk-test")
    monkeypatch.setattr("mini_pi.cli.app.create_llm", lambda *args, **kwargs: llm)

    result = runner.invoke(
        app,
        ["--cwd", str(tmp_path), "--no-session", "--no-banner", "--provider", "openai"],
        input="/bogus\n/exit\n",
    )

    assert result.exit_code == 0
    assert "unknown command" in result.output
    assert llm.calls == []


def test_startup_prefers_provider_with_saved_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """上次连接 provider 没有 Key 时，改用已保存 Key 的 provider 而不是卡住。"""
    monkeypatch.setattr(
        "mini_pi.cli.app.load_last_connection",
        lambda: ConnectionPreference(provider="openai", model="gpt-5.6-terra"),
    )
    monkeypatch.setattr(
        "mini_pi.cli.app.resolve_api_key",
        lambda provider, **kwargs: "sk-x" if provider == "deepseek" else None,
    )
    calls: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        "mini_pi.cli.app.create_llm",
        lambda provider, model=None, **kwargs: calls.append((provider, model))
        or FakeLLMClient([]),
    )

    result = runner.invoke(
        app, ["--cwd", str(tmp_path), "--no-session", "--no-banner"], input="/exit\n"
    )

    assert result.exit_code == 0, result.output
    assert calls == [("deepseek", "deepseek-flash")]
    assert "deepseek/deepseek-flash" in result.output


def test_connect_alias_still_switches_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """/connect 作为 /model 的兼容别名保留。"""
    calls: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        "mini_pi.cli.app.resolve_api_key", lambda *args, **kwargs: "sk-test"
    )
    monkeypatch.setattr(
        "mini_pi.cli.app.create_llm",
        lambda provider, model=None, **kwargs: calls.append((provider, model))
        or FakeLLMClient([]),
    )

    result = runner.invoke(
        app,
        ["--cwd", str(tmp_path), "--no-session", "--no-banner"],
        input="/connect deepseek deepseek-v4-pro\n/exit\n",
    )

    assert result.exit_code == 0, result.output
    assert ("deepseek", "deepseek-v4-pro") in calls


def test_prompt_and_build_saves_key_and_returns_agent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """启动补 Key 助手：验证通过即落盘并返回可运行 Agent。"""
    import io

    from rich.console import Console

    from mini_pi.cli.app import _prompt_and_build
    from mini_pi.cli.console import ConsoleRenderer
    from mini_pi.workspace.workspace import Workspace

    saved: list[tuple[str, str, str]] = []
    monkeypatch.setattr("mini_pi.cli.app.ask_api_key", lambda console, provider: "sk-test")
    monkeypatch.setattr(
        "mini_pi.cli.app.verify_credentials", lambda provider, key, model: None
    )
    monkeypatch.setattr(
        "mini_pi.cli.app.save_connection",
        lambda provider, key, model: saved.append((provider, key, model))
        or Path("/tmp/fake-auth.json"),
    )
    monkeypatch.setattr(
        "mini_pi.cli.app.create_llm", lambda provider, model=None, **kwargs: FakeLLMClient([])
    )
    console = Console(file=io.StringIO(), width=200, no_color=True)

    agent = _prompt_and_build(
        console=console,
        workspace=Workspace(tmp_path),
        max_steps=5,
        renderer=ConsoleRenderer(console),
        provider="openai",
        model="gpt-5.6-terra",
        no_session=True,
    )

    assert agent is not None
    assert saved == [("openai", "sk-test", "gpt-5.6-terra")]


def test_prompt_and_build_cancels_on_empty_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """空 Key 视为取消，不验证也不落盘。"""
    import io

    from rich.console import Console

    from mini_pi.cli.app import _prompt_and_build
    from mini_pi.cli.console import ConsoleRenderer
    from mini_pi.workspace.workspace import Workspace

    monkeypatch.setattr("mini_pi.cli.app.ask_api_key", lambda console, provider: None)
    monkeypatch.setattr(
        "mini_pi.cli.app.save_connection",
        lambda provider, key, model: pytest.fail("must not save without a key"),
    )
    console = Console(file=io.StringIO(), width=200, no_color=True)

    agent = _prompt_and_build(
        console=console,
        workspace=Workspace(tmp_path),
        max_steps=5,
        renderer=ConsoleRenderer(console),
        provider="openai",
        model="gpt-5.6-terra",
        no_session=True,
    )

    assert agent is None
