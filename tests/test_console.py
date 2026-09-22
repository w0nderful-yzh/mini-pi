from __future__ import annotations

import io

from rich.console import Console

from mini_pi.agent.events import (
    AgentEndEvent,
    MessageDeltaEvent,
    MessageEndEvent,
    MessageStartEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
)
from mini_pi.cli.console import ConsoleRenderer
from mini_pi.llm.types import AssistantMessage, ToolCall, Usage
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


def test_hides_thinking_and_clears_status_before_text() -> None:
    """思考内容不能泄漏；临时图案在正文开始时清理。"""
    stream = io.StringIO()
    renderer = ConsoleRenderer(Console(file=stream, force_terminal=True, width=80, no_color=True))
    renderer.handle(MessageStartEvent())
    assert renderer._thinking is not None
    renderer.handle(MessageDeltaEvent(kind="thinking", delta="secret reasoning"))
    renderer.handle(MessageDeltaEvent(kind="text", delta="answer"))
    renderer.handle(MessageEndEvent(message=AssistantMessage(content="answer")))
    output = stream.getvalue()
    # tty 流保留显示和清理的 ANSI 序列；画面上的图案已被清除。
    assert "db         db" in output
    assert "\x1b[2K" in output
    assert "secret reasoning" not in output
    assert "answer" in output
    assert renderer._thinking is None


def test_thinking_status_is_silent_without_tty_or_banner() -> None:
    """非终端和禁用图案时，只保留正文输出。"""
    for terminal, show_thinking in [(False, True), (True, False)]:
        stream = io.StringIO()
        renderer = ConsoleRenderer(
            Console(file=stream, force_terminal=terminal, width=80),
            show_thinking=show_thinking,
        )
        renderer.handle(MessageStartEvent())
        renderer.handle(MessageDeltaEvent(kind="thinking", delta="private"))
        renderer.handle(MessageEndEvent(message=AssistantMessage(content="")))
        assert "private" not in stream.getvalue()
        assert "db         db" not in stream.getvalue()


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


def test_renders_usage_when_present() -> None:
    """有 provider usage 时显示本轮 token 用量。"""
    renderer, stream = make_renderer()
    renderer.handle(
        MessageEndEvent(
            message=AssistantMessage(
                content="hi", usage=Usage(input_tokens=10, output_tokens=5, total_tokens=15)
            )
        )
    )
    assert "tokens: in 10 / out 5" in stream.getvalue()


def test_omits_usage_line_without_usage() -> None:
    """没有 usage（如 FakeLLM）时不打印 token 行。"""
    renderer, stream = make_renderer()
    renderer.handle(MessageEndEvent(message=AssistantMessage(content="hi")))
    assert "tokens:" not in stream.getvalue()


def test_renders_modified_files_summary() -> None:
    """工具改动文件时追加摘要，便于一眼确认改动范围。"""
    renderer, stream = make_renderer()
    call = ToolCall(id="c1", name="edit", arguments={"path": "a.py"})
    renderer.handle(
        ToolExecutionEndEvent(
            tool_call=call,
            result=ToolResult(content="Replaced 1 block(s)", modified_files=["a.py"]),
            is_error=False,
        )
    )
    assert "1 file(s) changed" in stream.getvalue()


def test_tool_arguments_stay_on_one_line() -> None:
    """超长参数要截断，工具调用块保持单行。"""
    renderer, stream = make_renderer()
    call = ToolCall(id="c1", name="write", arguments={"path": "a.txt", "content": "x" * 500})
    renderer.handle(ToolExecutionStartEvent(tool_call=call))
    output = stream.getvalue()
    assert "…" in output
    assert output.count("\n") == 1
