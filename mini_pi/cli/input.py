"""CLI 输入层：可选的 prompt_toolkit 行编辑与无依赖的单行回退。

这一层只负责把用户按下的内容读成文本，不持有 Agent/Session，也不写任何消息：
提交前的历史、补全、多行编辑都停留在终端侧，只有 `read()` 返回的完整文本才会
交给 REPL 决定是否作为一条 user 消息提交。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Protocol

# 输入历史与凭据同目录，目录 0700、文件 0600，且位于用户目录而不是项目目录
DEFAULT_HISTORY_PATH = Path.home() / ".mini-pi" / "history"

# 补全候选：与 /help 展示的命令保持同一集合，由测试防止漂移
SLASH_COMMANDS: tuple[str, ...] = (
    "/model",
    "/connect",
    "/compact",
    "/sessions",
    "/new",
    "/reset",
    "/status",
    "/context",
    "/tools",
    "/help",
    "/exit",
)

_REPL_PROMPT = "mini-pi> "
# 多行输入的第二行起使用续行提示，明确当前还在同一次提交中
_CONTINUATION_PROMPT = "   ... "


class ReplReader(Protocol):
    """REPL 输入契约：返回一次提交的完整文本，EOF/中断按异常冒泡。"""

    def read(self) -> str:
        """读取一次用户提交；EOFError 表示输入结束，KeyboardInterrupt 表示取消。"""
        ...


class BasicReplReader:
    """无依赖回退实现：调用内建 input()，保持原有单行 REPL 语义。"""

    def __init__(self, prompt: str = _REPL_PROMPT) -> None:
        """记录提示符；不预读、不缓存任何输入。"""
        self._prompt = prompt

    def read(self) -> str:
        """读取一行；UnicodeDecodeError/EOFError/KeyboardInterrupt 原样冒泡给调用方。"""
        return input(self._prompt)


class _SlashCommandCompleter:
    """仅在整行以 `/` 开头时补全命令名，不对任务文本做猜测。"""

    def __init__(self, commands: tuple[str, ...]) -> None:
        """持有候选命令列表。"""
        self._commands = commands

    def get_completions(self, document: Any, complete_event: Any) -> Any:
        """按当前输入前缀产出补全项；已进入参数部分则不再补全。"""
        from prompt_toolkit.completion import Completion

        text = document.text_before_cursor
        if not text.startswith("/") or any(char.isspace() for char in text):
            return
        for command in self._commands:
            if command.startswith(text):
                yield Completion(command, start_position=-len(text))


def build_slash_completer(commands: tuple[str, ...]) -> Any:
    """构造 prompt_toolkit Completer；继承真实基类才有异步补全接口。

    prompt_toolkit 在输入时会调用 `get_completions_async`，只提供一个同名普通方法
    的对象会在每次按键时报错，因此这里动态继承 `Completer`，同时保持模块顶层不导入。
    """
    from prompt_toolkit.completion import Completer

    class _ReplCompleter(_SlashCommandCompleter, Completer):
        """把命令补全实现接到 prompt_toolkit 的 Completer 契约上。"""

    return _ReplCompleter(commands)


class PromptToolkitReplReader:
    """prompt_toolkit 实现：持久历史、多行编辑、`/` 补全与 Ctrl+L 清屏。"""

    def __init__(
        self,
        *,
        history_path: Path = DEFAULT_HISTORY_PATH,
        prompt: str = _REPL_PROMPT,
    ) -> None:
        """延迟导入并装配 PromptSession；库缺失时抛 ImportError 由工厂回退。"""
        # 顶层不导入 prompt_toolkit：未安装时模块本身仍可被 CLI 加载
        from prompt_toolkit import PromptSession
        from prompt_toolkit.history import FileHistory, InMemoryHistory

        self._prompt = prompt
        self.history_path = history_path
        try:
            _prepare_history_file(history_path)
            history: Any = FileHistory(str(history_path))
        except OSError:
            # 历史文件不可写不应导致无法对话；降级为本次进程内历史
            history = InMemoryHistory()
        self.history = history
        self._session = PromptSession(
            multiline=True,
            history=history,
            completer=build_slash_completer(SLASH_COMMANDS),
            complete_while_typing=True,
            prompt_continuation=_CONTINUATION_PROMPT,
            key_bindings=_build_key_bindings(),
        )

    def read(self) -> str:
        """阻塞读取一次提交；Enter 提交，Ctrl+J/Alt+Enter 换行由键绑定提供。"""
        return self._session.prompt(self._prompt)


def _build_key_bindings() -> Any:
    """构造输入键位：Enter 提交、Ctrl+J 换行、Ctrl+L 清屏。"""
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.key_binding.bindings.named_commands import get_by_name

    bindings = KeyBindings()

    @bindings.add("enter")
    def _submit(event: Any) -> None:
        # 多行模式默认 Enter 换行；这里改为提交，换行改由 Ctrl+J 提供
        event.current_buffer.validate_and_handle()

    @bindings.add("c-j")
    def _newline(event: Any) -> None:
        # Ctrl+J 与 Alt+Enter 都是显式换行，不提交也不写历史
        event.current_buffer.insert_text("\n")

    @bindings.add("escape", "enter")
    def _alt_newline(event: Any) -> None:
        # Alt+Enter 与 Ctrl+J 语义一致，便于不同终端习惯
        event.current_buffer.insert_text("\n")

    @bindings.add("c-l")
    def _clear_screen(event: Any) -> None:
        # 复用 prompt_toolkit 自带清屏：只重绘终端，不影响已输入文本
        get_by_name("clear-screen").run(event)

    return bindings


def _prepare_history_file(history_path: Path) -> None:
    """确保历史目录 0700、文件 0600；文件不存在时创建，避免 umask 放宽权限。"""
    history_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not history_path.exists():
        descriptor = os.open(history_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        os.close(descriptor)
    try:
        os.chmod(history_path, 0o600)
    except OSError:
        # 某些文件系统不支持 chmod：保持现有权限而不是中断输入
        return


def create_repl_reader(
    *,
    interactive: bool | None = None,
    history_path: Path | None = None,
) -> ReplReader:
    """按终端与库可用性选择 reader：不可交互或库缺失时回退单行 input()。"""
    if interactive is None:
        interactive = _interactive_terminal()
    if not interactive:
        return BasicReplReader()
    try:
        return PromptToolkitReplReader(history_path=history_path or DEFAULT_HISTORY_PATH)
    except ImportError:
        # 输入库不可用是可预期降级：原单行 REPL 仍然完整可用
        return BasicReplReader()


def _interactive_terminal() -> bool:
    """判断 stdin 与 stdout 是否都是交互终端；管道、重定向与测试一律走回退实现。"""
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except (AttributeError, ValueError):
        # 测试捕获流可能没有 isatty：按非交互处理
        return False
