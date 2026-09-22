"""持久化会话交互命令与 M7.3 跨进程闭环的离线验收。"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from mini_pi.auth import save_connection
from mini_pi.cli.app import app
from mini_pi.errors import LLMError, SessionError
from mini_pi.llm.types import UserMessage
from mini_pi.session.jsonl import JsonlSession
from tests.conftest import FakeLLMClient, assistant

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolate_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """隔离用户认证和 Session 根目录，所有模型调用使用 FakeLLM。"""
    monkeypatch.setattr("mini_pi.session.jsonl._sessions_root", lambda path: tmp_path / "sessions")
    monkeypatch.setattr("mini_pi.cli.app.load_last_connection", lambda: None)


def session_files(tmp_path: Path) -> list[Path]:
    """读取本测试 workspace 下的 JSONL 文件。"""
    return sorted((tmp_path / "sessions").rglob("*.jsonl"))


def user_contents(session: JsonlSession) -> list[str]:
    """提取活动链中的用户任务，便于断言会话边界。"""
    return [
        message.content
        for message in session.replay().messages
        if isinstance(message, UserMessage)
    ]


def test_new_creates_independent_session_and_keeps_old_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """/new 使用新 id 与空 parent 开始，旧文件仍可严格加载。"""
    llm = FakeLLMClient([assistant("old answer"), assistant("new answer")])
    monkeypatch.setattr("mini_pi.cli.app.create_llm", lambda provider, model=None, **kwargs: llm)

    result = runner.invoke(app, ["--cwd", str(tmp_path)], input="old task\n/new\nnew task\n/exit\n")

    assert result.exit_code == 0, result.output
    files = session_files(tmp_path)
    assert len(files) == 2
    sessions = [JsonlSession.load(path, expected_cwd=tmp_path) for path in files]
    old = next(session for session in sessions if user_contents(session) == ["old task"])
    new = next(session for session in sessions if user_contents(session) == ["new task"])
    assert old.header.id != new.header.id
    assert old.entries[0].parent_id is None
    assert new.entries[0].parent_id is None
    assert not any(
        isinstance(message, UserMessage) and message.content == "old task"
        for message in llm.calls[1]
    )
    assert f"new session: {new.path}" in result.output


def test_new_creation_failure_preserves_current_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """新文件创建失败时，CLI 仍沿旧会话继续，不丢失已有历史。"""
    llm = FakeLLMClient([assistant("first"), assistant("second")])
    monkeypatch.setattr("mini_pi.cli.app.create_llm", lambda provider, model=None, **kwargs: llm)

    def fail_new(self: object, **kwargs: object) -> None:
        raise SessionError("disk unavailable")

    monkeypatch.setattr("mini_pi.cli.app.AgentSession.new", fail_new)

    result = runner.invoke(app, ["--cwd", str(tmp_path)], input="one\n/new\ntwo\n/exit\n")

    assert result.exit_code == 0, result.output
    assert "session creation failed: disk unavailable" in result.output
    files = session_files(tmp_path)
    assert len(files) == 1
    assert user_contents(JsonlSession.load(files[0])) == ["one", "two"]


def test_new_requires_saved_session_in_memory_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """纯内存模式不暗中创建持久化会话，提示继续使用 /reset。"""
    monkeypatch.setattr(
        "mini_pi.cli.app.create_llm",
        lambda provider, model=None, **kwargs: FakeLLMClient([]),
    )

    result = runner.invoke(app, ["--cwd", str(tmp_path), "--no-session"], input="/new\n/exit\n")

    assert result.exit_code == 0, result.output
    assert "/new requires a saved session; use /reset" in result.output
    assert session_files(tmp_path) == []


def test_connect_switches_model_on_same_chain_without_persisting_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """验证后的模型只作用于后续 entry；Key 仅进入 auth.json。"""
    old_llm = FakeLLMClient([assistant("before")])
    new_llm = FakeLLMClient([assistant("after")])
    secret = "sk-session-secret"
    auth_path = tmp_path / "auth.json"

    def make_llm(
        provider: str, model: str | None = None, *, api_key: str | None = None
    ) -> FakeLLMClient:
        if api_key is None:
            assert (provider, model) == ("openai", "gpt-5.6-terra")
            return old_llm
        assert (provider, model, api_key) == ("deepseek", "deepseek-flash", secret)
        return new_llm

    monkeypatch.setattr("mini_pi.cli.app.create_llm", make_llm)
    monkeypatch.setattr(
        "mini_pi.cli.app.ask_credentials", lambda console, default: ("deepseek", secret)
    )
    monkeypatch.setattr("mini_pi.cli.app.verify_credentials", lambda provider, key, model: None)
    monkeypatch.setattr(
        "mini_pi.cli.app.save_connection",
        lambda provider, key, model: save_connection(provider, key, model, path=auth_path),
    )

    result = runner.invoke(
        app, ["--cwd", str(tmp_path)], input="first\n/connect\nsecond\n/exit\n"
    )

    assert result.exit_code == 0, result.output
    files = session_files(tmp_path)
    assert len(files) == 1
    session = JsonlSession.load(files[0])
    assert user_contents(session) == ["first", "second"]
    assert [(entry.provider, entry.model) for entry in session.entries[:3]] == [
        ("openai", "gpt-5.6-terra")
    ] * 3
    assert all(
        (entry.provider, entry.model) == ("deepseek", "deepseek-flash")
        for entry in session.entries[3:]
    )
    assert any(
        isinstance(message, UserMessage) and message.content == "first"
        for message in new_llm.calls[0]
    )
    assert secret in auth_path.read_text(encoding="utf-8")
    assert secret not in files[0].read_text(encoding="utf-8")
    assert secret not in result.output


def test_resumed_session_connect_uses_active_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """从旧文件恢复后，/connect 同 provider 时沿用该活动链的自定义模型。"""
    session = JsonlSession.create(cwd=tmp_path, provider="deepseek", model="deepseek-reasoner")
    observed: list[tuple[str, str | None]] = []

    def make_llm(
        provider: str, model: str | None = None, *, api_key: str | None = None
    ) -> FakeLLMClient:
        observed.append((provider, model))
        return FakeLLMClient([])

    monkeypatch.setattr("mini_pi.cli.app.create_llm", make_llm)
    def ask_credentials(console: object, default: str) -> tuple[str, str]:
        assert default == "deepseek"
        return "deepseek", "sk-new"

    monkeypatch.setattr("mini_pi.cli.app.ask_credentials", ask_credentials)
    monkeypatch.setattr(
        "mini_pi.cli.app.verify_credentials",
        lambda provider, key, model: observed.append((provider, model)),
    )
    monkeypatch.setattr(
        "mini_pi.cli.app.save_connection",
        lambda provider, key, model: tmp_path / "auth.json",
    )

    result = runner.invoke(
        app, ["--cwd", str(tmp_path), "--resume", str(session.path)], input="/connect\n/exit\n"
    )

    assert result.exit_code == 0, result.output
    assert observed == [("deepseek", "deepseek-reasoner")] * 3
    assert JsonlSession.load(session.path).entries == ()


def test_connect_verification_failure_keeps_original_chain(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """验证失败不保存 Key、不切换模型，后续提问仍沿原链继续。"""
    llm = FakeLLMClient([assistant("first"), assistant("second")])
    saved: list[str] = []
    monkeypatch.setattr("mini_pi.cli.app.create_llm", lambda provider, model=None, **kwargs: llm)
    monkeypatch.setattr(
        "mini_pi.cli.app.ask_credentials", lambda console, default: ("deepseek", "bad-key")
    )

    def reject(provider: str, api_key: str, model: str | None) -> None:
        raise LLMError("401 unauthorized", retryable=False)

    monkeypatch.setattr("mini_pi.cli.app.verify_credentials", reject)
    monkeypatch.setattr(
        "mini_pi.cli.app.save_connection",
        lambda provider, key, model: saved.append(key) or tmp_path / "auth.json",
    )

    result = runner.invoke(app, ["--cwd", str(tmp_path)], input="one\n/connect\ntwo\n/exit\n")

    assert result.exit_code == 0, result.output
    assert "key verification failed" in result.output
    assert saved == []
    session = JsonlSession.load(session_files(tmp_path)[0])
    assert user_contents(session) == ["one", "two"]
    assert all(entry.provider == "openai" for entry in session.entries)


def test_connect_then_new_uses_switched_model_in_new_header(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """/new 继承当前运行模型，不从旧 Session header 恢复旧配置。"""
    old_llm = FakeLLMClient([])
    new_llm = FakeLLMClient([assistant("done")])
    monkeypatch.setattr(
        "mini_pi.cli.app.create_llm",
        lambda provider, model=None, **kwargs: old_llm if "api_key" not in kwargs else new_llm,
    )
    monkeypatch.setattr(
        "mini_pi.cli.app.ask_credentials", lambda console, default: ("deepseek", "sk-test")
    )
    monkeypatch.setattr("mini_pi.cli.app.verify_credentials", lambda provider, key, model: None)
    monkeypatch.setattr(
        "mini_pi.cli.app.save_connection",
        lambda provider, key, model: tmp_path / "auth.json",
    )

    result = runner.invoke(app, ["--cwd", str(tmp_path)], input="/connect\n/new\nhello\n/exit\n")

    assert result.exit_code == 0, result.output
    sessions = [JsonlSession.load(path) for path in session_files(tmp_path)]
    assert len(sessions) == 2
    old = next(session for session in sessions if not session.entries)
    new = next(session for session in sessions if session.entries)
    assert (old.header.provider, old.header.model) == ("openai", "gpt-5.6-terra")
    assert (new.header.provider, new.header.model) == ("deepseek", "deepseek-flash")


def test_m73_cross_process_resume_continues_original_parent_chain(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """完整闭环：退出后恢复，第二轮模型看见旧历史且追加原 leaf。"""
    first_llm = FakeLLMClient([assistant("first answer")])
    second_llm = FakeLLMClient([assistant("second answer")])
    clients = iter((first_llm, second_llm))
    monkeypatch.setattr(
        "mini_pi.cli.app.create_llm", lambda provider, model=None, **kwargs: next(clients)
    )

    first_result = runner.invoke(app, ["first task", "--cwd", str(tmp_path)])
    assert first_result.exit_code == 0, first_result.output
    path = session_files(tmp_path)[0]
    before = JsonlSession.load(path)
    first_leaf = before.leaf_id

    second_result = runner.invoke(
        app, ["second task", "--cwd", str(tmp_path), "--resume", str(path)]
    )

    assert second_result.exit_code == 0, second_result.output
    assert [
        message.content
        for message in second_llm.calls[0]
        if isinstance(message, UserMessage)
    ] == ["first task", "second task"]
    after = JsonlSession.load(path)
    assert after.entries[: len(before.entries)] == before.entries
    assert after.entries[len(before.entries)].parent_id == first_leaf
    assert after.entries[-1].message.content == "second answer"
    assert len(session_files(tmp_path)) == 1
