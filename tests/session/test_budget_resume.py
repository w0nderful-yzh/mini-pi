"""预算停止后的 Session 保持可恢复，下一次 run 使用新预算。"""

from __future__ import annotations

from pathlib import Path

from mini_pi.agent.events import AgentEndEvent, AgentEvent
from mini_pi.llm.types import AssistantMessage, ToolMessage, Usage, UserMessage
from mini_pi.session.jsonl import JsonlSession
from mini_pi.session.runtime import AgentSession
from mini_pi.tools.registry import ToolRegistry
from tests.conftest import EchoTool, FakeLLMClient, assistant, tool_call


def _reply(input_tokens: int, *, content: str = "", calls: list | None = None) -> AssistantMessage:
    """构造带输入用量的离线回复。"""
    return assistant(content, tool_calls=calls).model_copy(
        update={
            "usage": Usage(
                input_tokens=input_tokens,
                output_tokens=1,
                total_tokens=input_tokens + 1,
            )
        }
    )


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(EchoTool())
    return registry


def test_budget_limit_preserves_tool_pair_and_resume_continues_with_fresh_budget(
    tmp_path: Path,
) -> None:
    """停止点落在完整工具批次后；恢复不会重放工具，并重置单次任务预算。"""
    events: list[AgentEvent] = []
    first_llm = FakeLLMClient(
        [
            _reply(100, calls=[tool_call("c1", "echo", {"text": "one"})]),
            _reply(180, calls=[tool_call("c2", "echo", {"text": "two"})]),
        ]
    )
    runtime = AgentSession.create(
        cwd=tmp_path,
        llm=first_llm,
        registry=_registry(),
        provider="deepseek",
        model="deepseek-flash",
        sessions_root=tmp_path / "sessions",
        max_run_input_tokens=300,
        on_event=events.append,
    )

    runtime.run("collect facts")

    end = events[-1]
    assert isinstance(end, AgentEndEvent) and end.reason == "budget_limit"
    before = JsonlSession.load(runtime.path)
    assert [entry.message.role for entry in before.entries[-4:]] == [
        "assistant", "tool", "assistant", "tool"
    ]
    assert runtime.state.modified_files == {"echo/one.txt", "echo/two.txt"}
    assert not any(
        isinstance(entry.message, UserMessage) and "runtime_budget_notice" in entry.message.content
        for entry in before.entries
    )

    resumed_llm = FakeLLMClient([_reply(200, content="continued")])
    resumed = AgentSession.resume(
        runtime.path,
        registry=_registry(),
        cwd=tmp_path,
        llm_factory=lambda provider, model: resumed_llm,
        max_run_input_tokens=300,
    )
    result = resumed.run("continue from saved observations")

    assert result.content == "continued"
    assert len(resumed_llm.calls) == 1
    assert any(
        isinstance(message, ToolMessage) and message.content == "two"
        for message in resumed_llm.calls[0]
    )
    after = JsonlSession.load(runtime.path)
    assert after.entries[: len(before.entries)] == before.entries
    assert [entry.message.role for entry in after.entries[-2:]] == ["user", "assistant"]
    assert resumed.max_run_input_tokens == 300


def test_budget_configuration_is_runtime_only_and_new_session_keeps_cli_setting(
    tmp_path: Path,
) -> None:
    """预算不写 JSONL；/new 创建的运行时沿用本次 CLI 配置。"""
    runtime = AgentSession.create(
        cwd=tmp_path,
        llm=FakeLLMClient([]),
        registry=_registry(),
        provider="deepseek",
        model="deepseek-flash",
        sessions_root=tmp_path / "sessions",
        max_run_input_tokens=1234,
    )
    created = runtime.new(sessions_root=tmp_path / "sessions")

    assert created.max_run_input_tokens == 1234
    assert "max_run_input_tokens" not in runtime.path.read_text(encoding="utf-8")
    assert "max_run_input_tokens" not in created.path.read_text(encoding="utf-8")
