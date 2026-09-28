"""Session 在写入用户消息前按下一次完整请求检查窗口。"""

from __future__ import annotations

from pathlib import Path

import pytest

from mini_pi.context.policy import DEFAULT_RESERVE_TOKENS, KNOWN_CONTEXT_WINDOWS
from mini_pi.session.runtime import AgentSession
from mini_pi.tools.registry import ToolRegistry
from tests.conftest import FakeLLMClient


def test_pending_task_crosses_window_without_writing_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """当前投影尚小、待提交任务超窗时，预检失败不追加规则或用户消息。"""
    model = "test-pending-task-window"
    llm = FakeLLMClient([])
    runtime = AgentSession.create(
        cwd=tmp_path,
        llm=llm,
        registry=ToolRegistry(),
        provider="deepseek",
        model=model,
        sessions_root=tmp_path / "sessions",
    )
    short = runtime._estimate_next_request("short").input_tokens
    long_task = "x" * 2000
    large = runtime._estimate_next_request(long_task).input_tokens
    assert large > short + 100
    monkeypatch.setitem(KNOWN_CONTEXT_WINDOWS, model, short + 50 + DEFAULT_RESERVE_TOKENS)
    before = runtime.path.read_bytes()

    reply = runtime.run(long_task)

    assert reply.stop_reason == "error"
    assert reply.error_message is not None and "no safe cut point" in reply.error_message
    assert llm.calls == []
    assert runtime.path.read_bytes() == before
    assert runtime.state.messages == []
