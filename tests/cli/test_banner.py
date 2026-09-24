"""启动 Banner 资源保真、渲染与降级测试。"""

from __future__ import annotations

import io
import re
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from mini_pi.cli.app import app
from mini_pi.cli.banner import (
    FALLBACK,
    TAGLINE,
    banner_width,
    load_banner,
    render_banner,
    render_startup,
)
from tests.conftest import FakeLLMClient, assistant

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolate_auth(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Banner 测试不读写用户认证文件。"""
    monkeypatch.setattr("mini_pi.cli.app.load_last_connection", lambda: None)
    monkeypatch.setattr(
        "mini_pi.cli.app.save_last_connection",
        lambda provider, model: tmp_path / "auth.json",
    )


def make_console(*, width: int = 80, terminal: bool = True, color: bool = False) -> Console:
    """构造可断言的 Console；默认关色，便于匹配纯文本。"""
    return Console(file=io.StringIO(), force_terminal=terminal, width=width, no_color=not color)


def test_banner_asset_is_preserved() -> None:
    """Art 必须原样保留：17 行、最宽 58、771 字符、纯 ASCII、无行尾空格。"""
    banner = load_banner()
    lines = banner.rstrip("\n").split("\n")

    assert len(lines) == 17
    assert banner_width() == 58
    assert sum(len(line) for line in lines) == 771
    assert all(ord(char) < 128 for char in banner)
    assert all(line == line.rstrip(" ") for line in lines)
    assert lines[0] == "   /                       \\"
    assert lines[-1] == "                      |___||___|      |___||___|"


def test_wide_terminal_prints_art_and_tagline() -> None:
    """宽终端输出完整 Art 与中文标语。"""
    console = make_console(width=80)

    render_banner(console)

    output = re.sub(r"\x1b\[[0-9;]*m", "", console.file.getvalue())
    assert "No Bullshit," in output
    assert TAGLINE in output


def test_narrow_terminal_falls_back_to_single_line() -> None:
    """窄屏降级为单行，绝不触发自动换行。"""
    console = make_console(width=40)

    render_banner(console)

    output = console.file.getvalue()
    assert "No Bullshit," not in output
    assert FALLBACK in output


def test_non_terminal_falls_back_to_single_line() -> None:
    """管道/CI 等非 tty 场景同样降级。"""
    console = make_console(terminal=False)

    render_banner(console)

    output = console.file.getvalue()
    assert "No Bullshit," not in output
    assert FALLBACK in output


def test_disabled_banner_prints_nothing() -> None:
    """显式禁用时不输出任何内容。"""
    console = make_console()

    render_banner(console, enabled=False)

    assert console.file.getvalue() == ""


def test_tagline_is_bold_cyan() -> None:
    """标语使用加粗青色，NO_COLOR 由 Rich 负责处理。"""
    console = make_console(color=True)

    render_banner(console)

    assert "\x1b[1;36m" in console.file.getvalue()


def test_interactive_startup_includes_tagline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """交互启动（非 tty 测试环境走降级）也带标语。"""
    monkeypatch.setattr("mini_pi.cli.app.resolve_api_key", lambda *args, **kwargs: None)

    result = runner.invoke(app, ["--cwd", str(tmp_path)], input="/exit\n")

    assert result.exit_code == 0
    assert TAGLINE in result.output


def test_no_banner_flag_suppresses_art_and_tagline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """--no-banner 同时关闭 Art 与标语。"""
    monkeypatch.setattr("mini_pi.cli.app.resolve_api_key", lambda *args, **kwargs: None)

    result = runner.invoke(app, ["--cwd", str(tmp_path), "--no-banner"], input="/exit\n")

    assert result.exit_code == 0
    assert TAGLINE not in result.output
    assert "No Bullshit," not in result.output


def test_one_shot_does_not_print_banner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """一次性任务保持输出干净。"""
    monkeypatch.setattr("mini_pi.cli.app.resolve_api_key", lambda *args, **kwargs: "sk-test")
    monkeypatch.setattr(
        "mini_pi.cli.app.create_llm",
        lambda *args, **kwargs: FakeLLMClient([assistant("ok")]),
    )

    result = runner.invoke(
        app,
        ["hello", "--cwd", str(tmp_path), "--no-session", "--provider", "openai"],
    )

    assert result.exit_code == 0
    assert TAGLINE not in result.output


@pytest.mark.parametrize(("width", "terminal"), [(80, True), (28, True), (80, False)])
def test_startup_summary_is_compact_across_terminal_modes(
    width: int, terminal: bool
) -> None:
    """宽屏、窄屏与非 tty 都只展示紧凑元数据和帮助入口。"""
    console = make_console(width=width, terminal=terminal)

    render_startup(
        console,
        version="0.1.0",
        provider="deepseek",
        model="deepseek-flash",
        project="sample",
        session="12345678",
    )

    output = re.sub(r"\x1b\[[0-9;]*m", "", console.file.getvalue())
    assert "mini-pi 0.1.0" in output
    assert "deepseek/deepseek-flash" in output
    assert "sample" in output
    assert "session 12345678" in output
    assert "/help for commands" in output
    assert "commands:" not in output
