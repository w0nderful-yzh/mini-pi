"""Assistant 正文展示：TTY 按完整 Markdown 块渲染，非 TTY 输出可复现原文。"""

from __future__ import annotations

from collections.abc import Callable

from rich.console import Console
from rich.markdown import Markdown


class BodyPresenter:
    """消费正文分片，仅在完整块边界渲染，避免半截 Markdown 和重复输出。"""

    def __init__(self, console: Console, redact: Callable[[str], str]) -> None:
        """保存终端模式与脱敏器；每条 assistant 消息由 start 重置。"""
        self._console = console
        self._redact = redact
        self._rich = console.is_terminal and not console.is_dumb_terminal
        self.start()

    def start(self) -> None:
        """清空本条 assistant 的分片和未闭合块。"""
        self._raw = ""
        self._partial = ""
        self._block: list[str] = []
        self._in_fence = False

    def feed(self, delta: str) -> None:
        """只把完整段落或完整围栏代码块写入 TTY。"""
        self._raw += delta
        if not self._rich:
            return
        self._partial += delta
        while "\n" in self._partial:
            line, self._partial = self._partial.split("\n", 1)
            self._accept_line(line + "\n")

    def finish(self, content: str) -> None:
        """补齐无增量流的正文并冲刷最后一个未闭合段落。"""
        if not self._raw and content:
            self.feed(content)
        elif content.startswith(self._raw) and len(content) > len(self._raw):
            self.feed(content[len(self._raw) :])
        if not self._raw:
            return
        if self._rich:
            if self._partial:
                self._accept_line(self._partial)
                self._partial = ""
            self._flush()
        else:
            # 非 tty 是日志/管道：保留 Markdown 原文，并在完整消息后统一脱敏。
            self._console.print(self._redact(self._raw), markup=False, highlight=False, soft_wrap=True)

    def _accept_line(self, line: str) -> None:
        """围栏代码块成对保留；普通段落在空行处安全冲刷。"""
        stripped = line.strip()
        fence = stripped.startswith("```") or stripped.startswith("~~~")
        if fence and not self._in_fence and self._block:
            self._flush()
        self._block.append(line)
        if fence:
            self._in_fence = not self._in_fence
            if not self._in_fence:
                self._flush()
        elif not stripped and not self._in_fence:
            self._flush()

    def _flush(self) -> None:
        """以脱敏后的完整块渲染；块只清空一次，杜绝结束事件重复正文。"""
        text = "".join(self._block).strip("\n")
        self._block.clear()
        if text:
            self._console.print(Markdown(self._redact(text), code_theme="monokai"))
