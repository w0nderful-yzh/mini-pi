"""`/last` 从 Session 事实重建最近一次任务的工具调用（折叠模式的完整视图）。"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from mini_pi.cli import style
from mini_pi.cli.app import _last_options, app
from mini_pi.cli.status import render_last_run
from mini_pi.session.jsonl import JsonlSession
from mini_pi.session.runtime import AgentSession
from mini_pi.tools import build_default_registry
from mini_pi.tools.registry import ToolRegistry
from mini_pi.workspace.workspace import Workspace
from tests.conftest import FakeLLMClient, assistant, tool_call

runner = CliRunner()


def _console() -> tuple[Console, io.StringIO]:
    """固定宽高的非 tty Console：断言纯文本行。"""
    stream = io.StringIO()
    return Console(file=stream, width=200, no_color=True, highlight=False), stream


def _runtime(tmp_path: Path, script: list, registry: ToolRegistry) -> AgentSession:
    """用脚本化模型和给定注册表装配持久化会话，并跑完一次任务。"""
    runtime = AgentSession.create(
        cwd=tmp_path,
        llm=FakeLLMClient(script),
        registry=registry,
        provider="deepseek",
        model="deepseek-chat",
        sessions_root=tmp_path / "sessions",
    )
    runtime.run("do the task")
    return runtime


def test_last_lists_tool_calls_from_the_session(
    tmp_path: Path, echo_registry: ToolRegistry
) -> None:
    """成功、失败与改动文件都从活动链还原，标题与实时视图同源。"""
    runtime = _runtime(
        tmp_path,
        [
            assistant(tool_calls=[tool_call("c1", "echo", {"text": "hi"})]),
            assistant(tool_calls=[tool_call("c2", "fail", {"reason": "boom"})]),
            assistant("done"),
        ],
        echo_registry,
    )
    console, stream = _console()

    render_last_run(console, agent=runtime)

    assert stream.getvalue().splitlines() == [
        "Last run tool calls (2) · page 1/1:",
        f" 1. {style.MARK_OK} echo · 1 file(s) changed",
        f" 2. {style.MARK_FAILED} fail · ToolError: boom",
    ]


def test_last_marks_nonzero_shell_exit_as_failure(tmp_path: Path) -> None:
    """details 不落盘，但 shell 非零仍必须还原成失败，不能因为缺字段就报成功。"""
    runtime = _runtime(
        tmp_path,
        [
            assistant(tool_calls=[tool_call("c1", "bash", {"command": "false"})]),
            assistant("done"),
        ],
        build_default_registry(Workspace(tmp_path)),
    )
    console, stream = _console()

    render_last_run(console, agent=runtime)

    assert f"{style.MARK_FAILED} Run false · exit_code: 1" in stream.getvalue()


def test_last_full_adds_bounded_content(
    tmp_path: Path, echo_registry: ToolRegistry
) -> None:
    """`full` 追加有界内容预览，与 --verbose 同一展开口径。"""
    runtime = _runtime(
        tmp_path,
        [
            assistant(tool_calls=[tool_call("c1", "echo", {"text": "hi"})]),
            assistant("done"),
        ],
        echo_registry,
    )
    plain, plain_stream = _console()
    render_last_run(plain, agent=runtime)
    full, full_stream = _console()

    render_last_run(full, agent=runtime, full=True)

    assert len(full_stream.getvalue().splitlines()) > len(plain_stream.getvalue().splitlines())
    assert style.hanging("hi") in full_stream.getvalue()


def test_last_without_a_run_reports_none(tmp_path: Path) -> None:
    """还没有任何任务时不编造内容。"""
    console, stream = _console()

    render_last_run(console, agent=None)

    assert stream.getvalue().strip() == "Last run tool calls: none"


def test_last_command_in_repl_does_not_touch_the_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`/last` 是只读视图：不请求模型，也不写入新的 user 消息。"""
    monkeypatch.setattr("mini_pi.session.jsonl._sessions_root", lambda path: tmp_path / "sessions")
    monkeypatch.setattr("mini_pi.cli.app.load_last_connection", lambda: None)
    monkeypatch.setattr("mini_pi.cli.app.resolve_api_key", lambda *args, **kwargs: "sk-test")
    monkeypatch.setattr("mini_pi.cli.app.save_last_connection", lambda *args: tmp_path / "auth.json")
    llm = FakeLLMClient(
        [
            assistant(tool_calls=[tool_call("c1", "read", {"path": "a.txt"})]),
            assistant("done"),
        ]
    )
    monkeypatch.setattr("mini_pi.cli.app.create_llm", lambda *args, **kwargs: llm)
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")

    result = runner.invoke(
        app,
        ["--cwd", str(tmp_path), "--no-banner"],
        input="task\n/last\n/last full\n/exit\n",
    )

    assert result.exit_code == 0, result.output
    assert "Last run tool calls (1) · page 1/1:" in result.output
    assert f"1. {style.MARK_OK} Read a.txt · completed" in result.output
    # 两次 /last 都不产生新请求，也不写入新消息
    assert len(llm.calls) == 2
    entries = JsonlSession.load(next((tmp_path / "sessions").rglob("*.jsonl"))).entries
    assert [entry.message.role for entry in entries] == [
        "system", "user", "assistant", "tool", "assistant"
    ]


def test_last_single_call_full_and_redaction(
    tmp_path: Path, echo_registry: ToolRegistry
) -> None:
    """单项详情可读完整捕获内容，但已知凭据格式必须屏蔽。"""
    runtime = _runtime(
        tmp_path,
        [
            assistant(tool_calls=[tool_call("c1", "echo", {"text": "Bearer sk-secret"})]),
            assistant("done"),
        ],
        echo_registry,
    )
    console, stream = _console()

    render_last_run(console, agent=runtime, index=1, full=True)

    output = stream.getvalue()
    assert "Last run tool call 1/1:" in output
    assert "Bearer [REDACTED]" in output
    assert "sk-secret" not in output


def test_last_pages_many_calls(tmp_path: Path, echo_registry: ToolRegistry) -> None:
    """默认只列一页并提供明确续读入口，页码与单项编号稳定。"""
    calls = [tool_call(f"c{index}", "echo", {"text": str(index)}) for index in range(1, 22)]
    runtime = _runtime(tmp_path, [assistant(tool_calls=calls), assistant("done")], echo_registry)
    first, first_stream = _console()
    second, second_stream = _console()

    render_last_run(first, agent=runtime)
    render_last_run(second, agent=runtime, page=2)

    assert "Last run tool calls (21) · page 1/2:" in first_stream.getvalue()
    assert "More tool calls: /last page 2" in first_stream.getvalue()
    assert "21. ✓ echo" not in first_stream.getvalue()
    assert "21. ✓ echo" in second_stream.getvalue()


def test_last_command_forms_are_explicit() -> None:
    """按编号和页码查看是本地命令，错误参数不应流入模型。"""
    assert _last_options("/last") == (False, None, 1)
    assert _last_options("/last full") == (True, None, 1)
    assert _last_options("/last 3") == (False, 3, 1)
    assert _last_options("/last 3 full") == (True, 3, 1)
    assert _last_options("/last page 2") == (False, None, 2)
    assert _last_options("/last full page 2") == (True, None, 2)
    assert _last_options("/last nonsense") is None


def test_last_batch_full_keeps_short_preview(tmp_path: Path, echo_registry: ToolRegistry) -> None:
    """批量 full 仍限每项预览，并指向单项完整捕获。"""
    long_text = "\n".join(f"line-{index:02d}" for index in range(50))
    runtime = _runtime(
        tmp_path,
        [assistant(tool_calls=[tool_call("c1", "echo", {"text": long_text})]), assistant("done")],
        echo_registry,
    )
    console, stream = _console()

    render_last_run(console, agent=runtime, full=True)

    assert "line-00" in stream.getvalue()
    assert "line-49" not in stream.getvalue()
    assert "/last 1 full shows captured content" in stream.getvalue()
