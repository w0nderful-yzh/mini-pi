"""M8.1 正文展示：Markdown 块、流式分片与非 tty 原文。"""

from __future__ import annotations

import io
import re

from rich.cells import cell_len
from rich.console import Console

from mini_pi.agent.events import (
    MessageDeltaEvent,
    MessageEndEvent,
    MessageStartEvent,
    ToolExecutionEndEvent,
)
from mini_pi.cli.body import BodyPresenter
from mini_pi.cli.console import ConsoleRenderer
from mini_pi.llm.types import AssistantMessage, ToolCall
from mini_pi.tools.base import ToolResult

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _console(*, terminal: bool, width: int = 80) -> tuple[Console, io.StringIO]:
    """固定输出宽度与模式，便于检查内容没有被重复或泄漏。"""
    stream = io.StringIO()
    return Console(file=stream, force_terminal=terminal, no_color=True, width=width), stream


def test_tty_renders_fragmented_markdown_once() -> None:
    """强调、行内代码与围栏代码在闭合后渲染，分片边界不露原始标记。"""
    console, stream = _console(terminal=True, width=40)
    body = BodyPresenter(console, lambda value: value)
    chunks = ["修复 **数", "字** 与 `count`。\n\n", "```python\n", "print(1)\n", "```"]

    for chunk in chunks:
        body.feed(chunk)
    body.finish("".join(chunks))

    output = _ANSI.sub("", stream.getvalue())
    assert output.count("数字") == 1
    assert output.count("print(1)") == 1
    assert "**" not in output
    assert "```" not in output


def test_non_tty_preserves_source_and_redacts_across_chunks() -> None:
    """管道保留 Markdown 原文；分片拆开的凭据只在完整消息后脱敏。"""
    console, stream = _console(terminal=False)
    body = BodyPresenter(console, lambda value: value.replace("sk-secret", "[REDACTED]"))

    body.feed("**done** sk-")
    body.feed("secret")
    body.finish("**done** sk-secret")

    assert stream.getvalue() == "**done** [REDACTED]\n"


def test_renderer_uses_final_message_if_no_text_delta() -> None:
    """Provider 只发完整消息时正文仍显示一次，不凭 UI 自造模型事实。"""
    console, stream = _console(terminal=False)
    renderer = ConsoleRenderer(console)

    renderer.handle(MessageStartEvent())
    renderer.handle(MessageEndEvent(message=AssistantMessage(content="final **answer**")))

    assert stream.getvalue() == "final **answer**\n"


def test_renderer_does_not_duplicate_streamed_tail() -> None:
    """已显示的块与结束事件里的完整正文不能重复。"""
    console, stream = _console(terminal=True)
    renderer = ConsoleRenderer(console)
    renderer.handle(MessageStartEvent())
    renderer.handle(MessageDeltaEvent(kind="text", delta="First **one**.\n\n"))
    renderer.handle(MessageDeltaEvent(kind="text", delta="Second `two`."))
    renderer.handle(
        MessageEndEvent(message=AssistantMessage(content="First **one**.\n\nSecond `two`."))
    )

    output = _ANSI.sub("", stream.getvalue())
    assert output.count("First one") == 1
    assert output.count("Second two") == 1


def test_long_chinese_paragraph_wraps_at_terminal_width() -> None:
    """中文长段落在窄终端按显示格宽折行，不露出 Markdown 标记。"""
    console, stream = _console(terminal=True, width=40)
    body = BodyPresenter(console, lambda value: value)
    content = "**结论**：" + "项目路径与失败信息需要清晰展示。" * 8

    body.feed(content[:13])
    body.feed(content[13:])
    body.finish(content)

    lines = _ANSI.sub("", stream.getvalue()).splitlines()
    assert len(lines) > 1
    assert all(cell_len(line) <= 40 for line in lines)
    assert "**" not in "".join(lines)


def test_long_code_path_stays_within_terminal_width() -> None:
    """围栏代码里的长路径按终端宽度折行，不能冲出窄屏。"""
    console, stream = _console(terminal=True, width=40)
    body = BodyPresenter(console, lambda value: value)
    path = "/very/long/path/" + "subdirectory/" * 8 + "file.txt"
    content = f"```text\n{path}\n```"

    body.feed(content)
    body.finish(content)

    lines = _ANSI.sub("", stream.getvalue()).splitlines()
    assert all(cell_len(line) <= 40 for line in lines)
    assert "```" not in "".join(lines)


def test_verbose_log_is_bounded_and_redacted_before_preview() -> None:
    """长日志只显示前段与续读入口，分段末尾的凭据也不能泄漏。"""
    console, stream = _console(terminal=False)
    renderer = ConsoleRenderer(console, verbose=True)
    renderer.set_secrets(["sk-secret"])
    result = "\n".join(["Bearer sk-secret", *(f"line-{index:02d}" for index in range(50))])
    call = ToolCall(id="c1", name="read", arguments={"path": "log.txt"})

    renderer.handle(
        ToolExecutionEndEvent(tool_call=call, result=ToolResult(content=result), is_error=False)
    )

    output = stream.getvalue()
    assert "sk-secret" not in output
    assert "line-49" not in output
    assert "/last 1 full shows captured content" in output
