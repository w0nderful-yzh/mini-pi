"""M7.D2：CLI 层的输入提交与 Ctrl+C 取消语义。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mini_pi.cli.app import app
from mini_pi.llm.types import (
    AssistantMessage,
    Message,
    StreamEvent,
    TextDeltaEvent,
    ToolSchema,
)
from mini_pi.session.jsonl import JsonlSession
from tests.conftest import FakeLLMClient, assistant

runner = CliRunner()


class ScriptedReader:
    """脚本化 reader：按序返回文本；元素是异常时抛出，用于模拟 Ctrl+C 或 EOF。"""

    def __init__(self, items: list[object]) -> None:
        """保存脚本项；读完返回 EOFError，与真实 reader 的结束语义一致。"""
        self._items = list(items)
        self.reads = 0

    def read(self) -> str:
        """返回下一项文本或抛出脚本化异常。"""
        if not self._items:
            raise EOFError
        self.reads += 1
        item = self._items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return str(item)


class InterruptingStreamLLM:
    """流式阶段抛 KeyboardInterrupt，模拟任务执行中的 Ctrl+C。"""

    def __init__(self) -> None:
        """记录调用次数，便于断言取消后不再请求。"""
        self.calls: list[list[Message]] = []

    def stream(
        self, messages: list[Message], tools: list[ToolSchema] | None = None
    ) -> Iterator[StreamEvent]:
        """产出一次增量后中断，确认半截内容不进入 Session。"""
        self.calls.append(list(messages))
        yield TextDeltaEvent(delta="partial")
        raise KeyboardInterrupt

    def complete(
        self, messages: list[Message], tools: list[ToolSchema] | None = None
    ) -> AssistantMessage:
        """取消场景不提供 complete。"""
        raise AssertionError("complete must not be used")


@pytest.fixture(autouse=True)
def isolate_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """隔离认证与 Session 根目录；所有模型调用固定为 FakeLLM。"""
    monkeypatch.setattr("mini_pi.session.jsonl._sessions_root", lambda path: tmp_path / "sessions")
    monkeypatch.setattr("mini_pi.cli.app.load_last_connection", lambda: None)
    monkeypatch.setattr("mini_pi.cli.app.resolve_api_key", lambda *args, **kwargs: "sk-test")
    monkeypatch.setattr(
        "mini_pi.cli.app.save_last_connection",
        lambda provider, model: tmp_path / "auth.json",
    )


def session_files(tmp_path: Path) -> list[Path]:
    """读取本测试 workspace 下的 JSONL 文件。"""
    return sorted((tmp_path / "sessions").rglob("*.jsonl"))


def test_single_interrupt_at_prompt_only_warns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """空闲时第一次 Ctrl+C 只提示，不退出也不清空会话。"""
    monkeypatch.setattr(
        "mini_pi.cli.app.create_llm", lambda provider, model=None, **kwargs: FakeLLMClient([])
    )
    reader = ScriptedReader([KeyboardInterrupt(), "/exit"])
    monkeypatch.setattr("mini_pi.cli.app.create_repl_reader", lambda: reader)

    result = runner.invoke(app, ["--cwd", str(tmp_path), "--no-banner"])

    assert result.exit_code == 0, result.output
    assert "Press Ctrl+C again to exit." in result.output
    assert "exiting mini-pi." not in result.output
    assert reader.reads == 2


def test_two_consecutive_interrupts_exit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """连续两次 Ctrl+C 退出进程，第二次不需要再输入。"""
    monkeypatch.setattr(
        "mini_pi.cli.app.create_llm", lambda provider, model=None, **kwargs: FakeLLMClient([])
    )
    reader = ScriptedReader([KeyboardInterrupt(), KeyboardInterrupt()])
    monkeypatch.setattr("mini_pi.cli.app.create_repl_reader", lambda: reader)

    result = runner.invoke(app, ["--cwd", str(tmp_path), "--no-banner"])

    assert result.exit_code == 0, result.output
    assert "Press Ctrl+C again to exit." in result.output
    assert "exiting mini-pi." in result.output


def test_interrupt_during_task_keeps_session_and_returns_to_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """任务中 Ctrl+C：以 cancelled 结束但保留已提交 user 消息，下一次 Ctrl+C 才退出。"""
    llm = InterruptingStreamLLM()
    monkeypatch.setattr("mini_pi.cli.app.create_llm", lambda provider, model=None, **kwargs: llm)
    reader = ScriptedReader(["do work", KeyboardInterrupt()])
    monkeypatch.setattr("mini_pi.cli.app.create_repl_reader", lambda: reader)

    result = runner.invoke(app, ["--cwd", str(tmp_path), "--no-banner"])

    assert result.exit_code == 0, result.output
    assert "Task cancelled by user." in result.output
    # 任务取消后紧接着的空闲 Ctrl+C 命中“连续两次”语义，直接退出
    assert "exiting mini-pi." in result.output
    assert len(llm.calls) == 1
    messages = JsonlSession.load(session_files(tmp_path)[0]).replay().messages
    assert [message.role for message in messages] == ["system", "user"]
    assert messages[-1].content == "do work"


def test_multiline_submission_creates_one_user_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """多行编辑只在提交时产生一条 user 消息，换行保留在消息内容里。"""
    llm = FakeLLMClient([assistant("ok")])
    monkeypatch.setattr("mini_pi.cli.app.create_llm", lambda provider, model=None, **kwargs: llm)
    reader = ScriptedReader(["first line\nsecond line", "/exit"])
    monkeypatch.setattr("mini_pi.cli.app.create_repl_reader", lambda: reader)

    result = runner.invoke(app, ["--cwd", str(tmp_path), "--no-banner"])

    assert result.exit_code == 0, result.output
    users = [
        message.content
        for message in JsonlSession.load(session_files(tmp_path)[0]).replay().messages
        if message.role == "user"
    ]
    assert users == ["first line\nsecond line"]
    assert any(
        message.role == "user" and message.content == "first line\nsecond line"
        for message in llm.calls[0]
    )


def test_one_shot_interrupt_exits_with_sigint_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """一次性任务被取消时返回 128+SIGINT，不伪装成成功。"""
    llm = InterruptingStreamLLM()
    monkeypatch.setattr("mini_pi.cli.app.create_llm", lambda provider, model=None, **kwargs: llm)

    result = runner.invoke(app, ["do work", "--cwd", str(tmp_path), "--no-banner"])

    assert result.exit_code == 130, result.output
    assert "Task cancelled by user." in result.output


def test_interrupt_outside_loop_still_returns_to_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """中断落在 Loop 之前（如压缩摘要请求）时兜底提示，REPL 仍可继续。"""
    monkeypatch.setattr(
        "mini_pi.cli.app.create_llm", lambda provider, model=None, **kwargs: FakeLLMClient([])
    )

    def interrupt_run(self: object, task: str) -> None:
        """模拟 AgentSession.run 在进入 Loop 前被 Ctrl+C 打断。"""
        raise KeyboardInterrupt

    monkeypatch.setattr("mini_pi.cli.app.AgentSession.run", interrupt_run)
    reader = ScriptedReader(["task", "/exit"])
    monkeypatch.setattr("mini_pi.cli.app.create_repl_reader", lambda: reader)

    result = runner.invoke(app, ["--cwd", str(tmp_path), "--no-banner"])

    assert result.exit_code == 0, result.output
    assert "interrupted" in result.output
    # 没有退出：脚本里的 /exit 仍被读到
    assert reader.reads == 2
