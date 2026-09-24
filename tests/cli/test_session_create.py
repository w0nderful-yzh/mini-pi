"""CLI 默认 Session 与显式纯内存模式的离线验收。"""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from mini_pi.cli.app import app
from mini_pi.errors import MiniPiError, SessionError
from mini_pi.llm.types import UserMessage
from mini_pi.session.jsonl import JsonlSession
from tests.conftest import FakeLLMClient, assistant

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolate_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """隔离真实认证与 Session 目录，测试不得写入用户主目录。"""
    monkeypatch.setattr("mini_pi.cli.app.load_last_connection", lambda: None)
    monkeypatch.setattr("mini_pi.cli.app.resolve_api_key", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "mini_pi.cli.app.save_last_connection",
        lambda provider, model: tmp_path / "auth.json",
    )
    monkeypatch.setattr("mini_pi.session.jsonl._sessions_root", lambda path: tmp_path / "sessions")


def test_default_creates_loadable_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """默认一次性任务写入可严格加载的 JSONL 并展示目录及最终路径。"""
    monkeypatch.setattr(
        "mini_pi.cli.app.create_llm",
        lambda provider, model=None, **kwargs: FakeLLMClient([assistant("done")]),
    )

    result = runner.invoke(app, ["hello", "--cwd", str(tmp_path)])

    assert result.exit_code == 0, result.output
    files = list((tmp_path / "sessions").rglob("*.jsonl"))
    assert len(files) == 1
    session = JsonlSession.load(files[0], expected_cwd=tmp_path)
    assert [
        message.content
        for message in session.replay().messages
        if isinstance(message, UserMessage)
    ] == ["hello"]
    assert "Session storage:" in result.output
    assert f"Session path: {files[0]}" in result.output


def test_no_session_keeps_memory_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """显式禁用时仍可完成任务，但不创建 Session 目录。"""
    monkeypatch.setattr(
        "mini_pi.cli.app.create_llm",
        lambda provider, model=None, **kwargs: FakeLLMClient([assistant("done")]),
    )

    result = runner.invoke(app, ["hello", "--cwd", str(tmp_path), "--no-session"])

    assert result.exit_code == 0, result.output
    assert not (tmp_path / "sessions").exists()
    assert "Session path:" not in result.output


def test_session_creation_failure_is_visible_and_nonzero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """落盘启动失败必须停止一次性任务，不回退到易丢失的内存模式。"""
    monkeypatch.setattr(
        "mini_pi.cli.app.create_llm",
        lambda provider, model=None, **kwargs: FakeLLMClient([]),
    )

    def fail_create(**kwargs: object) -> None:
        raise SessionError("disk unavailable")

    monkeypatch.setattr("mini_pi.cli.app.AgentSession.create", fail_create)

    result = runner.invoke(app, ["hello", "--cwd", str(tmp_path)])

    assert result.exit_code == 1
    assert "session creation failed: disk unavailable" in result.output
    assert not (tmp_path / "sessions").exists()


def test_interactive_exit_leaves_loadable_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """交互模式正常退出后，文件仍可从磁盘加载并回放消息。"""
    monkeypatch.setattr(
        "mini_pi.cli.app.create_llm",
        lambda provider, model=None, **kwargs: FakeLLMClient([assistant("done")]),
    )

    result = runner.invoke(app, ["--cwd", str(tmp_path)], input="hello\n/exit\n")

    assert result.exit_code == 0, result.output
    files = list((tmp_path / "sessions").rglob("*.jsonl"))
    assert len(files) == 1
    session = JsonlSession.load(files[0], expected_cwd=tmp_path)
    assert session.replay().messages[-1].content == "done"
    assert f"session {str(session.header.id)[:8]}" in result.output
    assert "Session path:" not in result.output


def test_first_model_switch_creates_default_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """启动时缺 Key 仍允许首次 /model 配置，并从那一刻开始持久化。"""

    def create_llm(
        provider: str, model: str | None = None, *, api_key: str | None = None
    ) -> FakeLLMClient:
        if api_key is None:
            raise MiniPiError("openai API key is not configured")
        assert api_key == "sk-test"
        return FakeLLMClient([assistant("done")])

    monkeypatch.setattr("mini_pi.cli.app.create_llm", create_llm)
    monkeypatch.setattr("mini_pi.cli.app.resolve_api_key", lambda *args, **kwargs: None)
    monkeypatch.setattr("mini_pi.cli.app.ask_api_key", lambda console, provider: "sk-test")
    monkeypatch.setattr("mini_pi.cli.app.verify_credentials", lambda provider, key, model: None)
    monkeypatch.setattr(
        "mini_pi.cli.app.save_connection", lambda provider, key, model: tmp_path / "auth.json"
    )

    result = runner.invoke(
        app,
        ["--cwd", str(tmp_path), "--no-banner"],
        input="/model openai gpt-5.6-terra\nhello\n/exit\n",
    )

    assert result.exit_code == 0, result.output
    files = list((tmp_path / "sessions").rglob("*.jsonl"))
    assert len(files) == 1
    assert JsonlSession.load(files[0]).replay().messages[-1].content == "done"
    session = JsonlSession.load(files[0])
    assert f"session: {str(session.header.id)[:8]}" in result.output


def test_saved_session_reset_is_refused_without_changing_history(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """在 /new 实现前，不允许 /reset 只清内存而留下错误的可恢复历史。"""
    llm = FakeLLMClient([assistant("first"), assistant("second")])
    monkeypatch.setattr("mini_pi.cli.app.create_llm", lambda provider, model=None, **kwargs: llm)

    result = runner.invoke(app, ["--cwd", str(tmp_path)], input="one\n/reset\ntwo\n/exit\n")

    assert result.exit_code == 0, result.output
    assert "/reset is unavailable" in result.output
    assert any(
        isinstance(message, UserMessage) and message.content == "one"
        for message in llm.calls[1]
    )


def test_no_session_reset_preserves_phase1_behavior(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """纯内存模式的 /reset 仍在下一轮前清空旧用户任务。"""
    llm = FakeLLMClient([assistant("first"), assistant("second")])
    monkeypatch.setattr("mini_pi.cli.app.create_llm", lambda provider, model=None, **kwargs: llm)

    result = runner.invoke(
        app, ["--cwd", str(tmp_path), "--no-session"], input="one\n/reset\ntwo\n/exit\n"
    )

    assert result.exit_code == 0, result.output
    assert "context cleared" in result.output
    assert not any(
        isinstance(message, UserMessage) and message.content == "one"
        for message in llm.calls[1]
    )
