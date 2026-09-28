"""M7.9.1：一次性 CLI 的退出码必须如实反映本次 run 的终止原因。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mini_pi.cli.app import EXIT_CODES, app
from mini_pi.errors import LLMError
from mini_pi.llm.base import LLMClient
from mini_pi.session.jsonl import JsonlSession
from tests.conftest import (
    FakeLLMClient,
    InterruptingStreamLLM,
    ScriptedReader,
    assistant,
    tool_call,
)

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolate_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """隔离认证与 Session 根目录，所有请求使用脚本化模型，不联网。"""
    monkeypatch.setattr("mini_pi.session.jsonl._sessions_root", lambda path: tmp_path / "sessions")
    monkeypatch.setattr("mini_pi.cli.app.load_last_connection", lambda: None)
    monkeypatch.setattr("mini_pi.cli.app.resolve_api_key", lambda *args, **kwargs: "sk-test")
    monkeypatch.setattr("mini_pi.cli.app.save_last_connection", lambda *args: tmp_path / "auth.json")


def session_files(tmp_path: Path) -> list[Path]:
    """返回本测试 workspace 下已落盘的 JSONL 会话文件。"""
    return sorted((tmp_path / "sessions").rglob("*.jsonl"))


def _completed_llm() -> FakeLLMClient:
    """一轮回答即结束，用于 completed 与预算场景。"""
    return FakeLLMClient([assistant("done")])


def _error_llm() -> FakeLLMClient:
    """模型返回可预期错误，Loop 以 agent error 结束。"""
    return FakeLLMClient([LLMError("provider failed", retryable=False)])


def _step_limit_llm() -> FakeLLMClient:
    """每轮都请求工具：步数用尽也不会有最终回答，必须以 step_limit 结束。"""
    return FakeLLMClient(
        [assistant("partial", tool_calls=[tool_call("c1", "read", {"path": "a.txt"})])]
    )


@pytest.mark.parametrize(
    ("reason", "expected_code", "build_llm", "extra_args"),
    [
        # 退出码是对脚本的公开契约，这里显式写死，改动必须同步本表
        ("completed", 0, _completed_llm, []),
        ("error", 1, _error_llm, []),
        ("budget_limit", 2, _completed_llm, ["--max-run-input-tokens", "1"]),
        ("step_limit", 3, _step_limit_llm, ["--max-steps", "1"]),
        ("cancelled", 130, InterruptingStreamLLM, []),
    ],
)
def test_one_shot_exit_code_matches_terminal_reason(
    reason: str,
    expected_code: int,
    build_llm: Callable[[], LLMClient],
    extra_args: list[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """五种终止原因各自对应固定退出码；只有 completed 允许返回 0。"""
    (tmp_path / "a.txt").write_text("a\n", encoding="utf-8")
    llm = build_llm()
    monkeypatch.setattr("mini_pi.cli.app.create_llm", lambda *args, **kwargs: llm)

    result = runner.invoke(
        app, ["inspect", "--cwd", str(tmp_path), "--no-banner", *extra_args]
    )

    assert result.exit_code == expected_code, result.output
    # 映射表本身也要与对外契约一致，避免只在 CLI 分支里改对
    assert EXIT_CODES[reason] == expected_code
    if reason == "completed":
        assert "done" in result.output
    else:
        # 非完成原因必须在终端上可见，不能只体现在退出码里
        assert "incomplete" in result.output or reason in result.output.lower(), result.output


def test_step_limit_reports_limit_and_keeps_committed_tool_results(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """step_limit 明确说明上限与未完成，已提交的工具结果保留在 Session 中。"""
    (tmp_path / "a.txt").write_text("a\n", encoding="utf-8")
    llm = FakeLLMClient(
        [
            assistant("partial one", tool_calls=[tool_call("c1", "read", {"path": "a.txt"})]),
            assistant("partial two", tool_calls=[tool_call("c2", "read", {"path": "a.txt"})]),
        ]
    )
    monkeypatch.setattr("mini_pi.cli.app.create_llm", lambda *args, **kwargs: llm)

    result = runner.invoke(
        app, ["inspect", "--cwd", str(tmp_path), "--no-banner", "--max-steps", "2"]
    )

    assert result.exit_code == 3, result.output
    normalized = " ".join(result.output.split())
    assert "Reached the step limit (2) before finishing the task." in normalized
    assert "The task is incomplete" in normalized
    assert "raise --max-steps" in normalized
    # 两次请求都真的发生过；步数上限不是提前放弃
    assert len(llm.calls) == 2
    entries = JsonlSession.load(session_files(tmp_path)[0]).entries
    assert [entry.message.role for entry in entries[-4:]] == [
        "assistant",
        "tool",
        "assistant",
        "tool",
    ]


def test_step_limit_only_ends_current_task_in_repl(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """交互模式下 step_limit 只结束本次任务，下一条提问仍能正常完成。"""
    (tmp_path / "a.txt").write_text("a\n", encoding="utf-8")
    llm = FakeLLMClient(
        [
            assistant("working", tool_calls=[tool_call("c1", "read", {"path": "a.txt"})]),
            assistant("second task done"),
        ]
    )
    monkeypatch.setattr("mini_pi.cli.app.create_llm", lambda *args, **kwargs: llm)
    reader = ScriptedReader(["first task", "second task", "/exit"])
    monkeypatch.setattr("mini_pi.cli.app.create_repl_reader", lambda: reader)

    result = runner.invoke(app, ["--cwd", str(tmp_path), "--no-banner", "--max-steps", "1"])

    # 交互模式由用户 /exit 结束，进程退出码不表示任务成败
    assert result.exit_code == 0, result.output
    assert "Reached the step limit (1)" in " ".join(result.output.split())
    assert "second task done" in result.output
    assert reader.reads == 3, "step_limit 之后 REPL 必须继续读取输入"


def test_completed_task_exits_zero_without_incomplete_notice(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """正常完成保持退出码 0，且不会出现未完成提示。"""
    monkeypatch.setattr(
        "mini_pi.cli.app.create_llm", lambda *args, **kwargs: FakeLLMClient([assistant("done")])
    )

    result = runner.invoke(app, ["inspect", "--cwd", str(tmp_path), "--no-banner"])

    assert result.exit_code == 0, result.output
    assert "done" in result.output
    assert "incomplete" not in result.output
