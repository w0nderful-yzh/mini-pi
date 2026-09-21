from __future__ import annotations

import io

from rich.console import Console

from mini_pi.agent.events import (
    AgentEndEvent,
    MessageDeltaEvent,
    MessageEndEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
)
from mini_pi.cli.console import ConsoleRenderer
from mini_pi.llm.types import AssistantMessage, ToolCall
from mini_pi.tools.base import ToolResult


def make_renderer() -> tuple[ConsoleRenderer, io.StringIO]:
    """把 Rich 输出重定向到 StringIO，便于断言渲染结果。"""
    stream = io.StringIO()
    return ConsoleRenderer(Console(file=stream, width=200, no_color=True)), stream


def test_renders_text_deltas() -> None:
    """流式正文增量逐步打印，message_end 收尾换行。"""
    renderer, stream = make_renderer()
    renderer.handle(MessageDeltaEvent(kind="text", delta="Hel"))
    renderer.handle(MessageDeltaEvent(kind="text", delta="lo"))
    renderer.handle(MessageEndEvent(message=AssistantMessage(content="Hello")))
    assert "Hello" in stream.getvalue()


def test_renders_tool_starts_and_results() -> None:
    """工具调用展示名称+参数，结束后展示结果首行。"""
    renderer, stream = make_renderer()
    call = ToolCall(id="c1", name="bash", arguments={"command": "pytest"})
    renderer.handle(ToolExecutionStartEvent(tool_call=call))
    renderer.handle(
        ToolExecutionEndEvent(
            tool_call=call, result=ToolResult(content="exit_code: 0"), is_error=False
        )
    )
    output = stream.getvalue()
    assert "bash" in output
    assert "pytest" in output
    assert "exit_code: 0" in output


def test_renders_errors() -> None:
    """LLM 错误终止要显式展示错误信息。"""
    renderer, stream = make_renderer()
    renderer.handle(AgentEndEvent(reason="error", error="api down"))
    assert "api down" in stream.getvalue()


def test_preview_skips_blank_lines() -> None:
    """首行为空行时展示首个非空行；全空白内容显示 (empty)。"""
    renderer, stream = make_renderer()
    call = ToolCall(id="c1", name="read", arguments={})
    renderer.handle(
        ToolExecutionEndEvent(
            tool_call=call, result=ToolResult(content="\n  \nsecond line"), is_error=False
        )
    )
    renderer.handle(
        ToolExecutionEndEvent(tool_call=call, result=ToolResult(content="\n \n"), is_error=False)
    )
    output = stream.getvalue()
    assert "second line" in output
    assert "(empty)" in output


def test_renders_step_limit() -> None:
    """达到步数上限要有明确提示。"""
    renderer, stream = make_renderer()
    renderer.handle(AgentEndEvent(reason="step_limit"))
    assert "step limit" in stream.getvalue()
