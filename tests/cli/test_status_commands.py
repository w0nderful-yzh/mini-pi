"""状态与上下文命令只读当前会话，不写入模型历史。"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from mini_pi.cli.app import app
from mini_pi.llm.types import UserMessage
from mini_pi.session.jsonl import JsonlSession
from tests.conftest import FakeLLMClient, assistant

runner = CliRunner()


def test_status_context_and_tools_after_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """命令展示活动投影和短会话 id，且不会变成新的 user task。"""
    monkeypatch.setattr("mini_pi.session.jsonl._sessions_root", lambda path: tmp_path / "sessions")
    monkeypatch.setattr("mini_pi.cli.app.load_last_connection", lambda: None)
    monkeypatch.setattr("mini_pi.cli.app.resolve_api_key", lambda *args, **kwargs: "sk-test")
    monkeypatch.setattr("mini_pi.cli.app.save_last_connection", lambda *args: tmp_path / "auth.json")
    llm = FakeLLMClient([assistant("done")])
    monkeypatch.setattr("mini_pi.cli.app.create_llm", lambda *args, **kwargs: llm)

    result = runner.invoke(
        app,
        ["--cwd", str(tmp_path), "--no-banner"],
        input="task\n/status\n/status full\n/context\n/tools\n/exit\n",
    )

    assert result.exit_code == 0, result.output
    path = next((tmp_path / "sessions").rglob("*.jsonl"))
    session = JsonlSession.load(path)
    assert f"Session: {str(session.header.id)[:8]}" in result.output
    assert f"Session path: {path}" in result.output
    assert "Total (estimated):" in result.output
    assert "Auto-compaction: planned for M7.6" in result.output
    assert "read:" in result.output
    assert [item.content for item in session.replay().messages if isinstance(item, UserMessage)] == ["task"]
    assert len(llm.calls) == 1


def test_unknown_window_and_memory_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """未知窗口不给百分比，纯内存模式明确标识会话。"""
    monkeypatch.setattr("mini_pi.cli.app.load_last_connection", lambda: None)
    monkeypatch.setattr("mini_pi.cli.app.resolve_api_key", lambda *args, **kwargs: "sk-test")
    monkeypatch.setattr("mini_pi.cli.app.save_last_connection", lambda *args: tmp_path / "auth.json")
    monkeypatch.setattr("mini_pi.cli.app.create_llm", lambda *args, **kwargs: FakeLLMClient([]))

    result = runner.invoke(
        app,
        ["--cwd", str(tmp_path), "--no-session", "--model", "custom-model"],
        input="/status\n/context\n/exit\n",
    )

    assert result.exit_code == 0, result.output
    assert "Session: memory only" in result.output
    assert "window unknown" in result.output
    assert "Auto-compaction: unavailable" in result.output
