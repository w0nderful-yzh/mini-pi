"""状态与上下文命令只读当前会话，不写入模型历史。"""

from __future__ import annotations

from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from mini_pi.agent.agent import Agent
from mini_pi.cli import style
from mini_pi.cli.app import app
from mini_pi.cli.console import ConsoleRenderer
from mini_pi.cli.status import render_context
from mini_pi.llm.types import UserMessage
from mini_pi.session.jsonl import JsonlSession
from mini_pi.tools.registry import ToolRegistry
from tests.conftest import EchoTool, FakeLLMClient, assistant

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
    assert style.row("Session", str(session.header.id)[:8]) in result.output
    assert style.row("Session path", str(path)) in result.output
    assert style.row("Total", "~") in result.output
    assert style.row("Tool schemas", "~") in result.output
    assert style.row("Next request", "~") in result.output
    assert style.row("Compaction", "window threshold") in result.output
    assert "cost-aware early compaction" in result.output
    assert "read        " in result.output
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
    assert style.row("Session", "memory only") in result.output
    assert "window unknown" in result.output
    assert style.row("Next request", "~") in result.output
    assert style.row("Compaction", "unavailable") in result.output


def test_context_displays_the_same_request_prediction_as_agent(tmp_path: Path) -> None:
    """CLI 请求预测读取 Agent 的快照，分类总量独立列示。"""
    registry = ToolRegistry()
    registry.register(EchoTool())
    agent = Agent(
        llm=FakeLLMClient([]), registry=registry, cwd=tmp_path,
        provider="openai", model="custom-model",
    )
    agent.state.messages.append(UserMessage(content="task"))
    expected = agent.request_snapshot().input_tokens
    output = StringIO()
    console = Console(file=output, width=200)

    render_context(
        console, agent=agent, renderer=ConsoleRenderer(console)
    )

    assert style.row("Next request", f"~{expected} tokens") in output.getvalue()
    assert style.row("Tool schemas", "~") in output.getvalue()


def test_explicit_window_survives_model_switch_and_new(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLI 显式窗口和 reserve 在 /model、/new 后仍由同一策略展示。"""
    monkeypatch.setattr("mini_pi.session.jsonl._sessions_root", lambda path: tmp_path / "sessions")
    monkeypatch.setattr("mini_pi.cli.app.load_last_connection", lambda: None)
    monkeypatch.setattr("mini_pi.cli.app.resolve_api_key", lambda *args, **kwargs: "sk-test")
    monkeypatch.setattr("mini_pi.cli.app.save_last_connection", lambda *args: tmp_path / "auth.json")
    monkeypatch.setattr("mini_pi.cli.app.create_llm", lambda *args, **kwargs: FakeLLMClient([]))

    result = runner.invoke(
        app,
        ["--cwd", str(tmp_path), "--no-banner", "--model", "custom-model",
         "--context-window", "32000", "--reserve-tokens", "2000"],
        input="/status\n/context\n/model deepseek deepseek-flash\n/new\n/status\n/context\n/exit\n",
    )

    assert result.exit_code == 0, result.output
    threshold = style.row(
        "Compaction", "window threshold 30,000 tokens (window 32,000 - reserve 2,000)"
    )
    assert result.output.count(threshold) == 2
    assert result.output.count("/ 32,000 tokens") == 4  # /status 与 /context 各读两次同一窗口。
    assert "new session:" in result.output


def test_no_session_explicit_window_and_builtin_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """纯内存模式使用同一配置，未显式覆盖时切模型重算内置窗口。"""
    monkeypatch.setattr("mini_pi.cli.app.load_last_connection", lambda: None)
    monkeypatch.setattr("mini_pi.cli.app.resolve_api_key", lambda *args, **kwargs: "sk-test")
    monkeypatch.setattr("mini_pi.cli.app.save_last_connection", lambda *args: tmp_path / "auth.json")
    monkeypatch.setattr("mini_pi.cli.app.create_llm", lambda *args, **kwargs: FakeLLMClient([]))

    result = runner.invoke(
        app,
        ["--cwd", str(tmp_path), "--no-session", "--model", "custom-model",
         "--context-window", "32000", "--reserve-tokens", "2000"],
        input="/context\n/model deepseek deepseek-flash\n/context\n/exit\n",
    )
    assert result.exit_code == 0, result.output
    threshold = style.row(
        "Compaction", "window threshold 30,000 tokens (window 32,000 - reserve 2,000)"
    )
    assert result.output.count(threshold) == 2

    default = runner.invoke(
        app,
        ["--cwd", str(tmp_path), "--no-session", "--model", "custom-model"],
        input="/context\n/model deepseek deepseek-flash\n/context\n/exit\n",
    )
    assert default.exit_code == 0, default.output
    assert style.row("Compaction", "unavailable (context window unknown)") in default.output
    assert "window 1,000,000 - reserve 8,192" in default.output


def test_invalid_window_does_not_create_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """显式窗口小于默认 reserve 时，创建失败不留下空会话文件。"""
    monkeypatch.setattr("mini_pi.session.jsonl._sessions_root", lambda path: tmp_path / "sessions")
    monkeypatch.setattr("mini_pi.cli.app.load_last_connection", lambda: None)
    monkeypatch.setattr("mini_pi.cli.app.resolve_api_key", lambda *args, **kwargs: "sk-test")
    monkeypatch.setattr("mini_pi.cli.app.create_llm", lambda *args, **kwargs: FakeLLMClient([]))

    result = runner.invoke(
        app,
        ["task", "--cwd", str(tmp_path), "--context-window", "1000"],
    )

    assert result.exit_code == 1
    assert "reserve_tokens must be smaller than context_window" in result.output
    assert not (tmp_path / "sessions").exists()
