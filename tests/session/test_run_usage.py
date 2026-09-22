"""最近任务用量从完整 Session 活动链恢复。"""

from __future__ import annotations

from pathlib import Path

from mini_pi.errors import LLMError
from mini_pi.llm.types import AssistantMessage, SystemMessage, Usage, UserMessage
from mini_pi.session.jsonl import JsonlSession
from mini_pi.session.runtime import AgentSession
from mini_pi.session.usage import recent_session_run_usage
from mini_pi.tools.registry import ToolRegistry
from tests.conftest import EchoTool, FakeLLMClient, assistant, tool_call


def _reply(input_tokens: int, output_tokens: int, *, content: str = "") -> AssistantMessage:
    """给 FakeLLM 回复附上可精确核对的 Provider usage。"""
    return assistant(content).model_copy(
        update={
            "usage": Usage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
            )
        }
    )


def test_last_run_usage_survives_resume_and_model_switch(tmp_path: Path) -> None:
    """多请求求和、最近请求与工具数独立；恢复后不依赖 Renderer。"""
    registry = ToolRegistry()
    registry.register(EchoTool())
    first = assistant(tool_calls=[tool_call("c1", "echo", {"text": "hi"})]).model_copy(
        update={"usage": Usage(input_tokens=10, output_tokens=2, total_tokens=12)}
    )
    llm = FakeLLMClient([first, _reply(20, 3, content="done")])
    runtime = AgentSession.create(
        cwd=tmp_path,
        llm=llm,
        registry=registry,
        provider="deepseek",
        model="deepseek-flash",
        sessions_root=tmp_path / "sessions",
    )
    runtime.run("first task")
    usage = runtime.last_run_usage
    assert usage is not None
    assert (usage.requests, usage.measured_requests) == (2, 2)
    assert (usage.input_tokens, usage.output_tokens, usage.latest_input_tokens) == (30, 5, 20)
    assert usage.tool_calls == 1
    assert usage.duration_seconds is not None and usage.duration_seconds >= 0

    resumed = AgentSession.resume(
        runtime.path,
        registry=registry,
        llm_factory=lambda provider, model: FakeLLMClient([_reply(7, 1, content="next")]),
    )
    assert resumed.last_run_usage == usage
    resumed.set_llm(FakeLLMClient([_reply(7, 1, content="next")]), provider="openai", model="new")
    assert resumed.last_run_usage == usage
    resumed.run("second task")
    second = resumed.last_run_usage
    assert second is not None
    assert (second.requests, second.input_tokens, second.output_tokens) == (1, 7, 1)
    assert second.latest_input_tokens == 7
    assert second.tool_calls == 0
    assert JsonlSession.load(runtime.path).replay().model == "new"


def test_missing_usage_error_and_step_limit_are_visible(tmp_path: Path) -> None:
    """缺失的请求不算零成本；错误回复和 step limit 的请求仍计数。"""
    registry = ToolRegistry()
    registry.register(EchoTool())
    runtime = AgentSession.create(
        cwd=tmp_path,
        llm=FakeLLMClient([_reply(10, 1, content="ok"), LLMError("failed", retryable=False)]),
        registry=registry,
        provider="deepseek",
        model="deepseek-flash",
        sessions_root=tmp_path / "sessions",
    )
    runtime.run("first")
    runtime.run("second")
    failed = runtime.last_run_usage
    assert failed is not None
    assert (failed.requests, failed.measured_requests) == (1, 0)
    assert failed.latest_input_tokens is None

    limited = AgentSession.create(
        cwd=tmp_path,
        llm=FakeLLMClient([assistant(tool_calls=[tool_call("c2", "echo", {"text": "x"})])]),
        registry=registry,
        provider="deepseek",
        model="deepseek-flash",
        sessions_root=tmp_path / "sessions",
        max_steps=1,
    )
    limited.run("third")
    stopped = limited.last_run_usage
    assert stopped is not None
    assert (stopped.requests, stopped.measured_requests, stopped.tool_calls) == (1, 0, 1)


def test_compaction_does_not_erase_historical_run_usage(tmp_path: Path) -> None:
    """压缩投影不参与任务累计；统计仍沿原始 message entry 路径。"""
    session = JsonlSession.create(
        cwd=tmp_path,
        provider="deepseek",
        model="deepseek-flash",
        sessions_root=tmp_path / "sessions",
    )
    first = session.append_message(
        UserMessage(content="task"), provider="deepseek", model="deepseek-flash"
    )
    session.append_message(
        _reply(12, 2, content="done"), provider="deepseek", model="deepseek-flash"
    )
    session.append_compaction(
        summary="old task complete",
        first_kept_entry_id=first.id,
        tokens_before=100,
        system_message=SystemMessage(content="system"),
        usage=Usage(input_tokens=5, output_tokens=1, total_tokens=6),
    )
    stats = recent_session_run_usage(session.active_entries())
    assert stats is not None
    assert (stats.requests, stats.input_tokens, stats.output_tokens) == (1, 12, 2)
    resumed = AgentSession.resume(
        session.path,
        registry=ToolRegistry(),
        llm_factory=lambda provider, model: FakeLLMClient([]),
    )
    assert resumed.last_run_usage == stats
