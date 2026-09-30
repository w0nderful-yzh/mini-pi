"""M9.1：问询输入、终端显示与 CLI 装配的离线端到端测试。"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from mini_pi.agent.events import ToolExecutionStartEvent
from mini_pi.cli.app import app
from mini_pi.cli.console import ConsoleRenderer
from mini_pi.cli.interaction import create_interaction_handler
from mini_pi.interaction import InteractionRequest
from mini_pi.llm.types import ToolMessage
from mini_pi.session.jsonl import JsonlSession
from tests.conftest import FakeLLMClient, ScriptedReader, assistant, tool_call

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolate_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """隔离凭据和会话位置，不请求真实模型。"""
    monkeypatch.setattr("mini_pi.session.jsonl._sessions_root", lambda path: tmp_path / "sessions")
    monkeypatch.setattr("mini_pi.cli.app.load_last_connection", lambda: None)
    monkeypatch.setattr("mini_pi.cli.app.resolve_api_key", lambda *args, **kwargs: "sk-test")
    monkeypatch.setattr("mini_pi.cli.app.save_last_connection", lambda *args: tmp_path / "auth.json")


def script() -> FakeLLMClient:
    """模型只提问和结束，验证 CLI 的回答能进入下一请求。"""
    return FakeLLMClient([
        assistant(tool_calls=[tool_call("q1", "ask_user", {
            "question": "采用哪个方案？", "options": ["兼容", "替换"]
        })]), assistant("done"),
    ])


@pytest.mark.parametrize("mode", ["session", "memory", "repl"])
def test_cli_reads_selection_and_retries_locally(
    mode: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """新建、内存和 REPL 模式都接问询，空白和无效序号不调用模型。"""
    llm = script()
    monkeypatch.setattr("mini_pi.cli.app.create_llm", lambda *args, **kwargs: llm)
    monkeypatch.setattr("mini_pi.cli.interaction._interactive_terminal", lambda renderer: True)
    args = ["choose", "--cwd", str(tmp_path), "--no-banner"]
    if mode == "memory":
        args.append("--no-session")
    elif mode == "repl":
        args = args[1:]
        monkeypatch.setattr("mini_pi.cli.app.create_repl_reader", lambda: ScriptedReader([
            "choose", "/exit"
        ]))

    result = runner.invoke(app, args, input="\n" + "9" * 5000 + "\n2\n")

    assert result.exit_code == 0, result.output
    assert len(llm.calls) == 2
    tool = llm.calls[1][-1]
    assert isinstance(tool, ToolMessage) and json.loads(tool.content)["answer"] == "替换"
    assert "no default is selected" in result.output
    assert "Choose 1–2" in result.output
    assert "Decision needed" not in tool.content and "Answer ›" not in tool.content
    paths = list((tmp_path / "sessions").rglob("*.jsonl"))
    if mode == "memory":
        assert paths == []
    else:
        assert tool in JsonlSession.load(paths[0]).replay().messages


@pytest.mark.parametrize("interactive,answer,expected", [
    (False, "2\n", 1), (True, "/cancel\n", 130), (True, "", 130),
])
def test_unavailable_or_cancelled_cli_never_guesses(
    interactive: bool, answer: str, expected: int,
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """非 TTY、显式取消和 EOF 都返回非零，并保留成对的问询结果。"""
    llm = script()
    monkeypatch.setattr("mini_pi.cli.app.create_llm", lambda *args, **kwargs: llm)
    monkeypatch.setattr("mini_pi.cli.interaction._interactive_terminal", lambda renderer: interactive)
    result = runner.invoke(app, ["choose", "--cwd", str(tmp_path)], input=answer)
    assert result.exit_code == expected, result.output
    assert len(llm.calls) == 1
    path = next((tmp_path / "sessions").rglob("*.jsonl"))
    tool = JsonlSession.load(path).replay().messages[-1]
    assert isinstance(tool, ToolMessage) and tool.is_error
    assert "interactive terminal" in result.output if not interactive else "cancelled" in tool.content


def test_resume_cli_injects_current_question_handler(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """恢复的运行时也有问询通道，旧答案不会成为新问题的默认值。"""
    first = script()
    second = FakeLLMClient([
        assistant(tool_calls=[tool_call("q2", "ask_user", {"question": "新的决定？"})]),
        assistant("continued"),
    ])
    clients = iter([first, second])
    monkeypatch.setattr("mini_pi.cli.app.create_llm", lambda *args, **kwargs: next(clients))
    monkeypatch.setattr("mini_pi.cli.interaction._interactive_terminal", lambda renderer: True)
    result = runner.invoke(app, ["choose", "--cwd", str(tmp_path)], input="1\n")
    assert result.exit_code == 0, result.output
    path = next((tmp_path / "sessions").rglob("*.jsonl"))
    old = path.read_bytes()

    result = runner.invoke(app, ["continue", "--cwd", str(tmp_path), "--resume", str(path)],
                           input="保留原格式\n")

    assert result.exit_code == 0, result.output
    assert json.loads(second.calls[1][-1].content)["answer"] == "保留原格式"
    assert path.read_bytes().startswith(old)


def test_question_display_redacts_and_clears_live_without_mutating_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """渲染只改变显示，不能把脱敏选项或 Live 元数据作为用户答案。"""
    output = io.StringIO()
    renderer = ConsoleRenderer(Console(file=output, force_terminal=True, width=80))
    renderer.set_secrets(["secret-value"])
    call = tool_call("q1", "ask_user", {"question": "choose"})
    renderer.handle(ToolExecutionStartEvent(tool_call=call))
    assert renderer._activity is not None
    monkeypatch.setattr("mini_pi.cli.interaction._interactive_terminal", lambda renderer: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "2")
    request = InteractionRequest("[bold]选择\x1b[2J", ("第一项", "secret-value"))

    response = create_interaction_handler(renderer)(request)

    assert renderer._activity is None
    assert response.answer == "secret-value"
    assert "secret-value" not in output.getvalue()
    assert "[REDACTED]" in output.getvalue() and "[bold]选择" in output.getvalue()


def test_redirected_terminal_is_not_interactive(monkeypatch: pytest.MonkeyPatch) -> None:
    """stdout 管道即使 stdin 是 TTY 也不能消耗问询答案。"""
    from mini_pi.cli.interaction import _interactive_terminal

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    renderer = ConsoleRenderer(Console(file=io.StringIO(), force_terminal=False))
    assert not _interactive_terminal(renderer)
