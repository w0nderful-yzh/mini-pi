"""M7.D2：输入层（prompt_toolkit 与无依赖回退）的边界与安全属性。"""

from __future__ import annotations

import re
import stat
import sys
from pathlib import Path

import pytest

from mini_pi.cli.input import (
    SLASH_COMMANDS,
    BasicReplReader,
    PromptToolkitReplReader,
    _build_key_bindings,
    build_slash_completer,
    create_repl_reader,
)

pytest.importorskip("prompt_toolkit")

from prompt_toolkit.keys import Keys  # noqa: E402  （依赖可用性由上一行保证）


def test_non_interactive_streams_fall_back_to_single_line() -> None:
    """非 tty（管道/测试）使用内建 input，不进入行编辑模式。"""
    reader = create_repl_reader(interactive=False)
    assert isinstance(reader, BasicReplReader)


def test_missing_library_falls_back_to_single_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """输入库不可用是可预期降级：仍然返回可用的单行 reader。"""
    # sys.modules 中的 None 会让 import prompt_toolkit 抛 ImportError
    monkeypatch.setitem(sys.modules, "prompt_toolkit", None)
    reader = create_repl_reader(interactive=True, history_path=tmp_path / "history")
    assert isinstance(reader, BasicReplReader)


def test_basic_reader_uses_builtin_input(monkeypatch: pytest.MonkeyPatch) -> None:
    """回退实现保持原语义：调用内建 input 并返回原始文本。"""
    monkeypatch.setattr("builtins.input", lambda prompt="": f"<{prompt}>task")
    assert BasicReplReader().read() == "<mini-pi> >task"


def test_prompt_toolkit_reader_persists_history_with_strict_permissions(
    tmp_path: Path,
) -> None:
    """历史写入 ~/.mini-pi 风格的 0600 文件，目录 0700，且多行原样保存。"""
    history_path = tmp_path / "nested" / "history"
    reader = create_repl_reader(interactive=True, history_path=history_path)

    assert isinstance(reader, PromptToolkitReplReader)
    assert history_path.is_file()
    assert stat.S_IMODE(history_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(history_path.parent.stat().st_mode) == 0o700

    reader.history.append_string("first line\nsecond line")
    content = history_path.read_text(encoding="utf-8")
    assert "+first line" in content and "+second line" in content


def test_history_path_outside_workspace_by_default() -> None:
    """默认历史不属于项目目录，避免把任务文本写进仓库。"""
    from mini_pi.cli.input import DEFAULT_HISTORY_PATH

    assert DEFAULT_HISTORY_PATH.parts[-2:] == (".mini-pi", "history")


def test_key_bindings_cover_submit_newline_and_clear_screen() -> None:
    """键位契约：Enter 提交、Ctrl+J/Alt+Enter 换行、Ctrl+L 清屏。"""
    keys = {tuple(binding.keys) for binding in _build_key_bindings().bindings}
    assert (Keys.ControlM,) in keys
    assert (Keys.ControlJ,) in keys
    assert (Keys.ControlL,) in keys
    assert (Keys.Escape, Keys.ControlM) in keys


def test_slash_completion_candidates_match_help_text() -> None:
    """补全候选与 /help 展示的命令保持一致，避免文档漂移。"""
    from mini_pi.cli.app import _HELP_TEXT

    mentioned = set(re.findall(r"(?<![\w/])/[a-z]+", _HELP_TEXT))
    assert mentioned == set(SLASH_COMMANDS)


def test_slash_completer_only_completes_command_position() -> None:
    """只有整行仍在命令位置时才补全，不猜测任务参数。"""
    from prompt_toolkit.completion import CompleteEvent
    from prompt_toolkit.document import Document

    completer = build_slash_completer(SLASH_COMMANDS)
    event = CompleteEvent()

    completions = list(completer.get_completions(Document(text="/co"), event))
    assert [item.text for item in completions] == ["/connect", "/compact", "/context"]

    # 已经进入参数或普通文本时不产出补全
    assert list(completer.get_completions(Document(text="/model "), event)) == []
    assert list(completer.get_completions(Document(text="fix bug"), event)) == []


def test_slash_completer_supports_async_contract() -> None:
    """补全器必须继承真实 Completer：输入时 prompt_toolkit 走异步接口。"""
    from prompt_toolkit.completion import Completer

    completer = build_slash_completer(SLASH_COMMANDS)
    assert isinstance(completer, Completer)
    assert hasattr(completer, "get_completions_async")
