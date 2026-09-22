"""M7.C7 语义化工具事件与真实失败信息的渲染边界。"""

from __future__ import annotations

import io

import pytest
from rich.console import Console

from mini_pi.agent.events import AgentEndEvent, ToolExecutionEndEvent, ToolExecutionStartEvent
from mini_pi.cli.console import ConsoleRenderer
from mini_pi.llm.types import ToolCall
from mini_pi.tools.base import ToolResult


def _renderer(*, width: int = 160, verbose: bool = False, terminal: bool = False) -> tuple[ConsoleRenderer, io.StringIO]:
    """构造受控终端，避免主机宽度与颜色设置影响事件文本。"""
    output = io.StringIO()
    console = Console(file=output, force_terminal=terminal, width=width, no_color=True)
    return ConsoleRenderer(console, show_thinking=False, verbose=verbose), output


@pytest.mark.parametrize(
    ("name", "arguments", "expected"),
    [
        ("read", {"path": "src/agent.py"}, "Read src/agent.py"),
        ("search", {"pattern": "run_loop", "path": "mini_pi"}, "Search 'run_loop' in mini_pi"),
        ("git_diff", {"path": "README.md"}, "Inspect git diff for README.md"),
        ("bash", {"command": "git status --short"}, "Inspect git status"),
        ("bash", {"command": "git diff --check"}, "Inspect git diff"),
        ("bash", {"command": "rg -n run_loop mini_pi"}, "Search 'run_loop' with rg"),
        ("bash", {"command": "rg -n AgentSession mini_pi"}, "Search 'AgentSession' with rg"),
        ("bash", {"command": "uv run pytest -q"}, "Run pytest with uv"),
        ("bash", {"command": "npm run build"}, "Run frontend build (npm)"),
        ("bash", {"command": "pnpm test"}, "Run frontend tests (pnpm)"),
    ],
)
def test_known_actions_have_deterministic_titles(
    name: str, arguments: dict[str, object], expected: str
) -> None:
    """连续工具调用的意图可区分；只由工具名和参数生成标题。"""
    renderer, output = _renderer()
    renderer.handle(ToolExecutionStartEvent(tool_call=ToolCall(id="c1", name=name, arguments=arguments)))
    assert output.getvalue() == f"● {expected}\n"


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("curl https://example.test/health", "Run curl"),
        ("echo ok | sed -n 1p", "Run shell pipeline"),
        ("pytest && git status", "Run compound shell command"),
        ("OPENAI_API_KEY=abc123 curl https://example.test", "Run shell command (credentials hidden)"),
        ("curl -H 'Authorization: Bearer token123' https://example.test", "Run shell command (credentials hidden)"),
    ],
)
def test_unknown_compound_and_sensitive_commands_use_safe_titles(
    command: str, expected: str
) -> None:
    """未知脚本不猜测构建或测试结果，凭据不进入默认标题。"""
    renderer, output = _renderer()
    renderer.handle(
        ToolExecutionStartEvent(
            tool_call=ToolCall(id="c1", name="bash", arguments={"command": command})
        )
    )
    assert output.getvalue() == f"● {expected}\n"
    assert "abc123" not in output.getvalue()
    assert "token123" not in output.getvalue()


def test_non_tty_events_stay_one_line_and_redact_before_truncation() -> None:
    """路径中的换行与长凭据不能让默认日志破行或泄漏截断前缀。"""
    renderer, output = _renderer(width=32)
    secret = "sk-" + "x" * 240
    renderer.set_secrets([secret])
    call = ToolCall(id="c1", name="read", arguments={"path": f"src/one\n{secret}.py"})
    renderer.handle(ToolExecutionStartEvent(tool_call=call))
    renderer.handle(
        ToolExecutionEndEvent(
            tool_call=call,
            result=ToolResult(content="read complete"),
            is_error=False,
        )
    )
    lines = output.getvalue().splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("● Read src/one")
    assert "sk-xxx" not in output.getvalue()
    assert "[REDACTED]" in lines[0]


def test_result_redaction_precedes_preview_cutoff() -> None:
    """长凭据即使跨过预览上限，也不能露出可识别前缀。"""
    renderer, output = _renderer()
    secret = "sk-" + "y" * 240
    renderer.set_secrets([secret])
    call = ToolCall(id="c1", name="read", arguments={"path": "missing"})
    renderer.handle(
        ToolExecutionEndEvent(
            tool_call=call,
            result=ToolResult(content=f"ToolError: {secret} rejected"),
            is_error=True,
        )
    )
    assert "[REDACTED]" in output.getvalue()
    assert "sk-yyyy" not in output.getvalue()


def test_bash_failure_shows_exit_stderr_timeout_and_truncation_without_task_judgment() -> None:
    """非零码是工具事实；即使之后 Agent 完成也不能提前宣告任务失败。"""
    renderer, output = _renderer(width=200)
    call = ToolCall(id="c1", name="bash", arguments={"command": "npm run build"})
    renderer.handle(ToolExecutionStartEvent(tool_call=call))
    renderer.handle(
        ToolExecutionEndEvent(
            tool_call=call,
            result=ToolResult(
                content="exit_code: 2\nstdout:\n(empty)\nstderr:\nTypeScript error in app.ts\n[output truncated]",
                details={"exit_code": 2, "timed_out": True, "stderr_truncated": True},
            ),
            is_error=False,
        )
    )
    renderer.handle(AgentEndEvent(reason="completed"))
    text = output.getvalue()
    assert "● Run frontend build (npm)" in text
    assert "✗ shell exited 2 (timed out) (output truncated) · stderr: TypeScript error in app.ts" in text
    assert "build passed" not in text.lower()
    assert "continuing" not in text.lower()
    assert "Agent stopped with an error" not in text


def test_search_zero_matches_and_tool_error_have_distinct_meaning() -> None:
    """零匹配不是故障；ToolError 才展示失败并保留原因。"""
    renderer, output = _renderer()
    search = ToolCall(id="c1", name="search", arguments={"pattern": "absent"})
    renderer.handle(
        ToolExecutionEndEvent(
            tool_call=search,
            result=ToolResult(content="No matches found", details={"count": 0}),
            is_error=False,
        )
    )
    read = ToolCall(id="c2", name="read", arguments={"path": "missing.py"})
    renderer.handle(
        ToolExecutionEndEvent(
            tool_call=read,
            result=ToolResult(content="ToolError: not a file: missing.py"),
            is_error=True,
        )
    )
    assert "✓ 0 matches" in output.getvalue()
    assert "✗ failed: ToolError: not a file: missing.py" in output.getvalue()


def test_verbose_shows_captured_result_and_redacts_credential_forms() -> None:
    """详细模式保留命令与已捕获内容，同时屏蔽配置 Key 和常见显式凭据。"""
    renderer, output = _renderer(verbose=True, width=200)
    renderer.set_secrets(["sk-known"])
    call = ToolCall(
        id="c1",
        name="bash",
        arguments={"command": "curl --api-key=abc123 -H 'Authorization: Bearer token123'"},
    )
    renderer.handle(ToolExecutionStartEvent(tool_call=call))
    renderer.handle(
        ToolExecutionEndEvent(
            tool_call=call,
            result=ToolResult(
                content="exit_code: 1\nstdout:\n(empty)\nstderr:\nBearer sk-known rejected",
                details={"exit_code": 1},
            ),
            is_error=False,
        )
    )
    text = output.getvalue()
    assert "Run shell command (credentials hidden)" in text
    assert "stderr:" in text
    assert "abc123" not in text
    assert "token123" not in text
    assert "sk-known" not in text
    assert "[REDACTED]" in text
