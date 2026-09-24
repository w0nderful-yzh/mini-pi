"""CLI `/sessions` 只读展示当前 workspace 的严格 Session 列表。"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from mini_pi.cli.app import app
from mini_pi.llm.types import SystemMessage, UserMessage
from mini_pi.session.jsonl import JsonlSession
from tests.conftest import FakeLLMClient

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolate_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """隔离认证与 Session 根目录，列表测试不得调用真实模型。"""
    monkeypatch.setattr("mini_pi.session.jsonl._sessions_root", lambda path: tmp_path / "sessions")
    monkeypatch.setattr("mini_pi.cli.app.load_last_connection", lambda: None)
    monkeypatch.setattr("mini_pi.cli.app.resolve_api_key", lambda *args, **kwargs: "sk-test")
    monkeypatch.setattr(
        "mini_pi.cli.app.save_last_connection",
        lambda *args: tmp_path / "auth.json",
    )


def _saved_session(tmp_path: Path) -> JsonlSession:
    """创建一个带摘要的可恢复 Session。"""
    session = JsonlSession.create(
        cwd=tmp_path,
        provider="deepseek",
        model="deepseek-flash",
    )
    system = session.append_message(
        SystemMessage(sections={"preamble": "rules"}),
        provider="deepseek",
        model="deepseek-flash",
        step_count=0,
    )
    kept = session.append_message(
        UserMessage(content="keep"),
        provider="deepseek",
        model="deepseek-flash",
        step_count=0,
    )
    session.append_compaction(
        summary="summary",
        first_kept_entry_id=kept.id,
        tokens_before=100,
        system_message=system.message,
    )
    return session


def test_sessions_lists_metadata_without_model_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """纯内存 CLI 也能只读列出保存记录，且不发送任何 LLM 请求。"""
    session = _saved_session(tmp_path)
    llm = FakeLLMClient([])
    monkeypatch.setattr("mini_pi.cli.app.create_llm", lambda *args, **kwargs: llm)

    result = runner.invoke(
        app,
        ["--cwd", str(tmp_path), "--no-session", "--no-banner"],
        input="/help\n/sessions\n/exit\n",
    )

    assert result.exit_code == 0, result.output
    assert "Saved sessions for" in result.output
    assert "/sessions                  list validated sessions" in result.output
    assert str(session.header.id)[:8] in result.output
    assert "deepseek/deepseek-flash" in result.output
    assert "summary yes" in result.output
    normalized = " ".join(result.output.split())
    assert "--continue resumes the latest validated session" in normalized
    assert "--resume <session.jsonl> resumes an exact file" in normalized
    assert llm.calls == []


def test_sessions_reports_corrupt_candidate_without_calling_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """列表复用严格候选校验，损坏文件不被跳过或传给模型。"""
    session = _saved_session(tmp_path)
    session.path.with_name("broken.jsonl").write_text("{broken}\n", encoding="utf-8")
    llm = FakeLLMClient([])
    monkeypatch.setattr("mini_pi.cli.app.create_llm", lambda *args, **kwargs: llm)

    result = runner.invoke(
        app,
        ["--cwd", str(tmp_path), "--no-session", "--no-banner"],
        input="/sessions\n/exit\n",
    )

    assert result.exit_code == 0, result.output
    assert "session listing failed: invalid session candidate" in result.output
    assert "Saved sessions for" not in result.output
    assert llm.calls == []


def test_sessions_marks_current_session_and_startup_hides_full_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """持久化启动只显示项目与短 id，列表用星号标出当前 Session。"""
    llm = FakeLLMClient([])
    monkeypatch.setattr("mini_pi.cli.app.create_llm", lambda *args, **kwargs: llm)

    result = runner.invoke(
        app,
        ["--cwd", str(tmp_path), "--no-banner"],
        input="/sessions\n/exit\n",
    )

    assert result.exit_code == 0, result.output
    path = next((tmp_path / "sessions").rglob("*.jsonl"))
    session_id = str(JsonlSession.load(path).header.id)[:8]
    assert f"project {tmp_path.name}" in result.output
    assert f"session {session_id}" in result.output
    assert f"* {session_id}" in result.output
    assert "* current session" in result.output
    assert "Session storage:" not in result.output
    assert "Session path:" not in result.output
    assert "commands:" not in result.output
    assert llm.calls == []
