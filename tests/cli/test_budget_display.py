"""CLI 显示单次任务预算，并为未完成的一次性任务返回非零码。"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from mini_pi.cli.app import app
from mini_pi.llm.types import AssistantMessage, Usage, UserMessage
from mini_pi.session.jsonl import JsonlSession
from tests.conftest import FakeLLMClient, assistant, tool_call

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolate_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """隔离认证与 Session，所有请求使用脚本化模型。"""
    monkeypatch.setattr("mini_pi.session.jsonl._sessions_root", lambda path: tmp_path / "sessions")
    monkeypatch.setattr("mini_pi.cli.app.load_last_connection", lambda: None)
    monkeypatch.setattr("mini_pi.cli.app.resolve_api_key", lambda *args, **kwargs: "sk-test")
    monkeypatch.setattr("mini_pi.cli.app.save_last_connection", lambda *args: tmp_path / "auth.json")


def _reply(input_tokens: int, *, content: str = "", calls: list | None = None) -> AssistantMessage:
    """生成可预测累计预算的回复。"""
    return assistant(content, tool_calls=calls).model_copy(
        update={
            "usage": Usage(
                input_tokens=input_tokens,
                output_tokens=1,
                total_tokens=input_tokens + 1,
            )
        }
    )


def test_one_shot_budget_limit_is_explicit_nonzero_and_session_is_resumable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """预算停止不显示完成态，一次性命令返回 2，完整工具结果已经落盘。"""
    llm = FakeLLMClient(
        [
            _reply(10_000, calls=[tool_call("c1", "read", {"path": "README.md"})]),
            _reply(18_000, calls=[tool_call("c2", "read", {"path": "AGENTS.md"})]),
        ]
    )
    monkeypatch.setattr("mini_pi.cli.app.create_llm", lambda *args, **kwargs: llm)

    result = runner.invoke(
        app,
        [
            "inspect",
            "--cwd", str(Path.cwd()),
            "--no-banner",
            "--max-run-input-tokens", "30000",
        ],
    )

    assert result.exit_code == 2, result.output
    normalized = " ".join(result.output.split())
    assert "Run input budget is close:" in normalized
    assert "Run stopped before the next model request" in normalized
    assert "The task is incomplete; continue in this session" in normalized
    assert len(llm.calls) == 2
    path = next((tmp_path / "sessions").rglob("*.jsonl"))
    session = JsonlSession.load(path)
    assert [entry.message.role for entry in session.entries[-4:]] == [
        "assistant", "tool", "assistant", "tool"
    ]


def test_interactive_continue_gets_fresh_budget_and_status_shows_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """预算只终止当前 task；REPL 下一条用户消息使用重置后的预算。"""
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b", encoding="utf-8")
    llm = FakeLLMClient(
        [
            _reply(10_000, calls=[tool_call("c1", "read", {"path": "a.txt"})]),
            _reply(18_000, calls=[tool_call("c2", "read", {"path": "b.txt"})]),
            _reply(20_000, content="continued"),
        ]
    )
    monkeypatch.setattr("mini_pi.cli.app.create_llm", lambda *args, **kwargs: llm)

    result = runner.invoke(
        app,
        ["--cwd", str(tmp_path), "--no-banner", "--max-run-input-tokens", "30000"],
        input="first\n/status\nsecond\n/exit\n",
    )

    assert result.exit_code == 0, result.output
    assert "Run input budget: 30000 tokens (request-boundary, resets per task)" in result.output
    assert "Run stopped before the next model request" in result.output
    assert "continued" in result.output
    assert len(llm.calls) == 3
    path = next((tmp_path / "sessions").rglob("*.jsonl"))
    users = [
        entry.message.content
        for entry in JsonlSession.load(path).entries
        if isinstance(entry.message, UserMessage)
    ]
    assert users == ["first", "second"]
    assert all("runtime_budget_notice" not in item for item in users)


def test_budget_defaults_off_and_cli_rejects_nonpositive_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """默认不启用预算；Typer 在运行时创建前拒绝非法值。"""
    monkeypatch.setattr(
        "mini_pi.cli.app.create_llm",
        lambda *args, **kwargs: FakeLLMClient([assistant("done")]),
    )
    result = runner.invoke(
        app,
        ["--cwd", str(tmp_path), "--no-session", "--no-banner"],
        input="/status\n/exit\n",
    )
    assert result.exit_code == 0, result.output
    assert "Run input budget: disabled" in result.output

    invalid = runner.invoke(app, ["--max-run-input-tokens", "0"])
    assert invalid.exit_code != 0
    assert "Invalid value" in invalid.output
