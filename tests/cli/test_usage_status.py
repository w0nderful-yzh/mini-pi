"""CLI 的任务累计用量与当前上下文分开展示。"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from mini_pi.cli.app import app
from mini_pi.llm.types import AssistantMessage, Usage
from mini_pi.session.jsonl import JsonlSession
from tests.conftest import FakeLLMClient, assistant, tool_call

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolate_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """测试用的 Session、认证和模型均与用户配置隔离。"""
    monkeypatch.setattr("mini_pi.session.jsonl._sessions_root", lambda path: tmp_path / "sessions")
    monkeypatch.setattr("mini_pi.cli.app.load_last_connection", lambda: None)
    monkeypatch.setattr("mini_pi.cli.app.resolve_api_key", lambda *args, **kwargs: "sk-test")
    monkeypatch.setattr("mini_pi.cli.app.save_last_connection", lambda *args: tmp_path / "auth.json")


def _with_usage(
    message: AssistantMessage, input_tokens: int, output_tokens: int
) -> AssistantMessage:
    """构造请求级实际用量，供 CLI 展示断言。"""
    return message.model_copy(
        update={
            "usage": Usage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
            )
        }
    )


def test_status_context_and_resume_use_persisted_request_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """两次请求累计与最后一次输入不同，恢复后仍显示原始统计。"""
    (tmp_path / "file.txt").write_text("hello\n", encoding="utf-8")
    llm = FakeLLMClient(
        [
            _with_usage(assistant(tool_calls=[tool_call("c1", "read", {"path": "file.txt"})]), 10, 2),
            _with_usage(assistant("done"), 20, 3),
        ]
    )
    monkeypatch.setattr("mini_pi.cli.app.create_llm", lambda *args, **kwargs: llm)
    first = runner.invoke(
        app,
        ["--cwd", str(tmp_path), "--no-banner"],
        input="task\n/status\n/context\n/exit\n",
    )
    assert first.exit_code == 0, first.output
    assert "requests 2 · provider in 30 / out 5 (2/2 usage) · tools 1" in first.output
    assert "Last run Provider usage: in 30 / out 5 tokens (2/2 requests reported usage)" in first.output
    assert "Last request Provider input: 20 tokens" in first.output
    assert "Last run requests: 2" in first.output
    assert "Last run tools: 1" in first.output
    assert "Current context (estimated):" in first.output
    assert "Total (estimated):" in first.output
    path = next((tmp_path / "sessions").rglob("*.jsonl"))
    before = JsonlSession.load(path).entries

    monkeypatch.setattr("mini_pi.cli.app.create_llm", lambda *args, **kwargs: FakeLLMClient([]))
    second = runner.invoke(
        app,
        ["--cwd", str(tmp_path), "--resume", str(path), "--no-banner"],
        input="/status\n/context\n/exit\n",
    )
    assert second.exit_code == 0, second.output
    assert "Last run Provider usage: in 30 / out 5 tokens (2/2 requests reported usage)" in second.output
    assert "Last request Provider input: 20 tokens" in second.output
    assert "Last run recorded span:" in second.output
    assert JsonlSession.load(path).entries == before


def test_memory_mode_marks_partial_usage_and_unknown_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """混合 usage 不报完整总成本；未知模型窗口不虚构百分比。"""
    llm = FakeLLMClient(
        [
            _with_usage(assistant(tool_calls=[tool_call("c1", "read", {"path": "missing"})]), 10, 2),
            assistant("done"),
        ]
    )
    monkeypatch.setattr("mini_pi.cli.app.create_llm", lambda *args, **kwargs: llm)
    result = runner.invoke(
        app,
        ["--cwd", str(tmp_path), "--no-session", "--model", "custom-model", "--no-banner"],
        input="task\n/status\n/context\n/reset\n/status\n/exit\n",
    )
    assert result.exit_code == 0, result.output
    # Rich 在窄终端可能换行，逐段断言覆盖率和部分实测标识。
    normalized = " ".join(result.output.split())
    assert "partial measured: in 10 / out 2 tokens (1/2 requests reported usage)" in normalized
    assert "Last request Provider input: unavailable" in result.output
    assert "window unknown" in result.output
    assert "Last run Provider usage: unavailable (no model requests)" in result.output
    assert "Last run elapsed:" in result.output
