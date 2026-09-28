"""M7.9.4 CLI 视觉契约：状态标记、列对齐、宽度与降级行为。

这些用例只断言展示层契约：颜色绝不单独承载状态，标签列对齐统一，窄屏/非 tty 输出
确定性文本。视觉细节（具体色号、间距）由 tty 人工验收记录覆盖。
"""

from __future__ import annotations

import io
import re
from pathlib import Path

import pytest
from rich.cells import cell_len
from rich.console import Console

from mini_pi.agent.events import (
    AgentEndEvent,
    AgentStartEvent,
    BudgetWarningEvent,
    MessageEndEvent,
    MessageStartEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
)
from mini_pi.cli import style
from mini_pi.cli.banner import render_startup
from mini_pi.cli.console import ConsoleRenderer
from mini_pi.cli.sessions import render_sessions
from mini_pi.cli.status import render_status
from mini_pi.llm.types import AssistantMessage, ToolCall
from mini_pi.tools.base import ToolResult
from mini_pi.workspace.workspace import Workspace
from tests.conftest import assistant, tool_call

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def make_console(*, width: int = 100, terminal: bool = False) -> tuple[Console, io.StringIO]:
    """固定宽高的 Console；显式高度让 Rich 不探测宿主终端。"""
    stream = io.StringIO()
    console = Console(
        file=stream,
        force_terminal=terminal,
        width=width,
        height=24,
        no_color=True,
        highlight=False,
    )
    return console, stream


def make_renderer(
    *, width: int = 100, terminal: bool = False, verbose: bool = False
) -> tuple[ConsoleRenderer, io.StringIO]:
    """把渲染器与输出流成对返回，供事件级用例使用。"""
    console, stream = make_console(width=width, terminal=terminal)
    return ConsoleRenderer(console, verbose=verbose), stream


def render_events(renderer: ConsoleRenderer, *events: object) -> None:
    """按顺序把一个 run 的事件喂给渲染器。"""
    for event in events:
        renderer.handle(event)  # type: ignore[arg-type]


class _StateCase:
    """一种需要可读展示的终止/工具状态。"""

    def __init__(self, name: str, mark: str, events: list[object]) -> None:
        """保存用例名、期望行首标记与事件序列。"""
        self.name = name
        self.mark = mark
        self.events = events


def _tool_end(name: str, arguments: dict[str, object], content: str, **details: object):
    """构造一次工具结束事件，details 直接透传给 UI 摘要。"""
    call = ToolCall(id="c1", name=name, arguments=arguments)
    return ToolExecutionEndEvent(
        tool_call=call,
        result=ToolResult(content=content, details=details or None),
        is_error=False,
    )


_STATE_CASES = (
    _StateCase(
        "shell_ok",
        style.MARK_OK,
        [_tool_end("bash", {"command": "pytest"}, "exit_code: 0", exit_code=0)],
    ),
    _StateCase(
        "shell_nonzero",
        style.MARK_FAILED,
        [_tool_end("bash", {"command": "pytest"}, "exit_code: 1", exit_code=1)],
    ),
    _StateCase(
        "shell_timeout",
        style.MARK_FAILED,
        [
            _tool_end(
                "bash",
                {"command": "sleep 9"},
                "exit_code: -1",
                exit_code=-1,
                timed_out=True,
            )
        ],
    ),
    _StateCase(
        "tool_error",
        style.MARK_FAILED,
        [
            ToolExecutionEndEvent(
                tool_call=ToolCall(id="c1", name="read", arguments={"path": "gone.py"}),
                result=ToolResult(content="ToolError: not a file: gone.py"),
                is_error=True,
            )
        ],
    ),
    _StateCase("completed", "Completed", [AgentEndEvent(reason="completed")]),
    _StateCase("step_limit", style.MARK_WARNING, [AgentEndEvent(reason="step_limit", step_limit=5)]),
    _StateCase(
        "budget_limit",
        style.MARK_WARNING,
        [
            AgentEndEvent(
                reason="budget_limit",
                budget_limit=100,
                budget_used=90,
                predicted_next_input=20,
                budget_source="provider",
            )
        ],
    ),
    _StateCase("error", style.MARK_FAILED, [AgentEndEvent(reason="error", error="api down")]),
    _StateCase("cancelled", style.MARK_FAILED, [AgentEndEvent(reason="cancelled")]),
    _StateCase(
        "budget_warning",
        style.MARK_WARNING,
        [
            BudgetWarningEvent(
                limit=100, used=90, remaining=10, predicted_next_input=20, source="provider"
            )
        ],
    ),
)


@pytest.mark.parametrize("case", _STATE_CASES, ids=[case.name for case in _STATE_CASES])
def test_every_state_carries_a_textual_marker(case: _StateCase) -> None:
    """每种状态都要有符号或结果词；去掉颜色后仍能读出发生了什么。"""
    renderer, stream = make_renderer()

    render_events(renderer, AgentStartEvent(), *case.events)

    output = stream.getvalue()
    assert case.mark in output, output


def test_thinking_frames_cycle_at_constant_width() -> None:
    """帧序列按时间推进，且每帧显示宽度一致——Live 精确清除依赖这一点。"""
    period = len(style.THINKING_FRAMES) / style.THINKING_FPS
    lines = [style.thinking_line(index * period / len(style.THINKING_FRAMES)) for index in
             range(len(style.THINKING_FRAMES))]

    assert [line[0] for line in lines] == list(style.THINKING_FRAMES)
    assert len({cell_len(line) for line in lines}) == 1
    # 帧宽度恒定还要求每个字形都是单格宽，否则终端里会折行、清除错位
    assert all(cell_len(frame) == 1 for frame in style.THINKING_FRAMES)
    assert style.thinking_line(0.0) == style.thinking_line(period)


def test_tool_result_line_repeats_the_action_title() -> None:
    """长会话里结果行必须自带操作标题，不能只写 completed。"""
    renderer, stream = make_renderer()
    call = ToolCall(id="c1", name="read", arguments={"path": "src/app.py"})

    render_events(
        renderer,
        ToolExecutionStartEvent(tool_call=call),
        ToolExecutionEndEvent(
            tool_call=call, result=ToolResult(content="ok"), is_error=False
        ),
    )

    lines = stream.getvalue().splitlines()
    assert lines[0] == f"{style.MARK_RUNNING} Read src/app.py"
    assert lines[1] == f"{style.MARK_OK} Read src/app.py · completed"


def test_summary_line_orders_metrics_and_groups_thousands() -> None:
    """收尾行按结果 → 工具 → 请求 → 用量 → 耗时排列，数值加千位分隔。"""
    renderer, stream = make_renderer()
    renderer._input_tokens = 35_689
    renderer._output_tokens = 1_587
    renderer._has_usage = True
    renderer._measured_requests = 16
    renderer._requests = 16
    renderer.last_tool_count = 20
    renderer.last_run_seconds = 15.1

    renderer.handle(AgentEndEvent(reason="completed"))

    assert (
        "Completed · 20 tools · 16 requests · in 35,689 / out 1,587 · 15.1s"
        in stream.getvalue()
    )


def test_partial_and_missing_usage_are_labelled() -> None:
    """部分实测与完全缺失的用量都不能写成总成本。"""
    renderer, stream = make_renderer()
    renderer._input_tokens = 10
    renderer._output_tokens = 2
    renderer._has_usage = True
    renderer._measured_requests = 1
    renderer._requests = 2

    renderer.handle(AgentEndEvent(reason="completed"))

    assert "in 10 / out 2 (partial 1/2)" in stream.getvalue()

    other, other_stream = make_renderer()
    other.handle(AgentEndEvent(reason="completed"))
    assert "provider usage unavailable" in other_stream.getvalue()


def test_status_rows_share_one_value_column(tmp_path: Path) -> None:
    """`/status` 的每个值都在同一列开始，标签宽度由 style 统一。"""
    console, stream = make_console()

    render_status(
        console,
        agent=None,
        provider="deepseek",
        model="deepseek-flash",
        cwd=tmp_path,
        renderer=ConsoleRenderer(console),
    )

    lines = stream.getvalue().splitlines()
    assert lines, "status 必须至少有可读信息"
    for line in lines:
        # 标签列宽固定：第 LABEL_WIDTH 个字符起是值，前一列必须是分隔空格
        assert line[style.LABEL_WIDTH - 1] == " ", line
        assert line[style.LABEL_WIDTH] != " ", line


def test_narrow_and_dumb_terminals_use_deterministic_field_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """窄屏、TERM=dumb 与非 tty 都不依赖宽度右对齐，输出逐字段成行。"""
    for name, width, terminal, term in (
        ("narrow", 28, True, "xterm-256color"),
        ("dumb", 80, True, "dumb"),
        ("pipe", 80, False, "xterm-256color"),
    ):
        monkeypatch.setenv("TERM", term)
        console, stream = make_console(width=width, terminal=terminal)

        render_startup(
            console,
            version="0.1.0",
            provider="deepseek",
            model="deepseek-flash",
            project="sample-project",
            session="a1b2c3d4",
        )

        # tty 分支会带 ANSI 样式；比较文本内容，去掉转义序列
        lines = [
            line for line in _ANSI.sub("", stream.getvalue()).splitlines() if line.strip()
        ]
        assert lines == [
            "mini-pi 0.1.0",
            "model deepseek/deepseek-flash",
            "project sample-project",
            "session a1b2c3d4",
            "/help for commands",
        ], name


def test_wide_identity_bar_fits_the_terminal_width() -> None:
    """宽屏身份栏把帮助入口右对齐在同一行，且不超过终端宽度。"""
    for width in (80, 120):
        console, stream = make_console(width=width, terminal=True)

        render_startup(
            console,
            version="0.1.0",
            provider="deepseek",
            model="deepseek-flash",
            project="sample-project",
            session="a1b2c3d4",
        )

        lines = [line for line in _ANSI.sub("", stream.getvalue()).splitlines() if line.strip()]
        assert lines[0] == "mini-pi 0.1.0 · deepseek/deepseek-flash"
        assert lines[1].endswith("/help for commands")
        assert len(lines[1]) == width, lines[1]


def test_session_list_aligns_summary_column(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`/sessions` 行按模型列宽对齐，摘要列不会因模型名长短而错位。"""
    from mini_pi.session.jsonl import JsonlSession

    monkeypatch.setattr("mini_pi.session.jsonl._sessions_root", lambda path: tmp_path / "sessions")
    for provider, model in (("deepseek", "deepseek-flash"), ("openai", "gpt-5.6-terra")):
        session = JsonlSession.create(
            cwd=Workspace(tmp_path).root, provider=provider, model=model
        )
        session.append_message(assistant("hi"), provider=provider, model=model, step_count=1)
    console, stream = make_console(width=120)

    render_sessions(console, cwd=tmp_path, current_session_id=None)

    rows = [line for line in stream.getvalue().splitlines() if "summary" in line]
    assert len(rows) == 2
    assert len({line.index("· summary") for line in rows}) == 1, rows


def test_tool_result_titles_use_arguments_not_colour() -> None:
    """工具标题由真实参数生成，默认不含整段参数或凭据。"""
    renderer, stream = make_renderer(width=200)
    call = ToolCall(id="c1", name="bash", arguments={"command": "uv run pytest -q"})

    render_events(
        renderer,
        ToolExecutionStartEvent(tool_call=call),
        _tool_end("bash", {"command": "uv run pytest -q"}, "exit_code: 0", exit_code=0),
    )

    lines = stream.getvalue().splitlines()
    assert lines == [
        f"{style.MARK_RUNNING} Run pytest with uv",
        f"{style.MARK_OK} Run pytest with uv · shell exited 0",
    ]


def test_dumb_terminal_keeps_per_tool_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    """TERM=dumb 不跑活动区也不折叠：逐行事实是它唯一能保证的输出。"""
    monkeypatch.setenv("TERM", "dumb")
    renderer, stream = make_renderer(terminal=True)
    call = ToolCall(id="c1", name="read", arguments={"path": "a.py"})

    render_events(
        renderer,
        AgentStartEvent(),
        ToolExecutionStartEvent(tool_call=call),
        ToolExecutionEndEvent(tool_call=call, result=ToolResult(content="ok"), is_error=False),
        AgentEndEvent(reason="completed"),
    )

    output = stream.getvalue()
    assert f"{style.MARK_RUNNING} Read a.py" in output
    assert f"{style.MARK_OK} Read a.py · completed" in output
    assert "/last for the" not in output


def test_verbose_tty_keeps_per_tool_lines_next_to_the_activity_line() -> None:
    """--verbose 在 tty 上仍逐行打印：活动区只是补充，不替代审计线索。"""
    renderer, stream = make_renderer(terminal=True, verbose=True)
    call = ToolCall(id="c1", name="read", arguments={"path": "a.py"})

    render_events(
        renderer,
        AgentStartEvent(),
        MessageStartEvent(),
        MessageEndEvent(message=AssistantMessage(content="thinking")),
        ToolExecutionStartEvent(tool_call=call),
        ToolExecutionEndEvent(tool_call=call, result=ToolResult(content="ok"), is_error=False),
        AgentEndEvent(reason="completed"),
    )

    output = stream.getvalue()
    # verbose 追加密钥已脱敏的参数，逐行事实完整保留
    assert f"{style.MARK_RUNNING} Read a.py" in output
    assert f"{style.MARK_OK} Read a.py" in output
    assert "completed" in output
    assert "/last for the" not in output
    # 活动区同时存在：等待模型时显示思考帧
    assert any(frame in output for frame in style.THINKING_FRAMES)


def test_non_tty_flow_has_no_ansi_and_keeps_one_line_per_event() -> None:
    """非 tty 只输出确定性文本行，不注入 ANSI 控制序列。"""
    renderer, stream = make_renderer(width=60)
    call = tool_call("c1", "read", {"path": "a.py"})

    render_events(
        renderer,
        AgentStartEvent(),
        ToolExecutionStartEvent(tool_call=call),
        ToolExecutionEndEvent(
            tool_call=call, result=ToolResult(content="x" * 500), is_error=False
        ),
        AgentEndEvent(reason="completed"),
    )

    output = stream.getvalue()
    assert "\x1b[" not in output
    # 长内容被单行截断，不产生额外换行
    assert output.count("\n") == 3, output
