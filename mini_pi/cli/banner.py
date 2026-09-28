"""启动 Banner：Art 原样加载，宽终端完整输出，窄屏/非 tty 降级单行。"""

from __future__ import annotations

from importlib import resources

from rich.cells import cell_len
from rich.console import Console

from mini_pi.cli import style

_ASSET_PACKAGE = "mini_pi"
_ASSET_RELATIVE_PATH = "assets/banner.txt"
TAGLINE = "牛人，就用牛的 coding agent！"
FALLBACK = f"mini-pi — {TAGLINE}"


def load_banner() -> str:
    """读取 Art 原文；不做 strip、格式化或重新生成。"""
    return (
        resources.files(_ASSET_PACKAGE)
        .joinpath(_ASSET_RELATIVE_PATH)
        .read_text(encoding="utf-8")
    )


def banner_width() -> int:
    """Art 的最大显示宽度；Art 为纯 ASCII，按字符数计即可。"""
    return max(len(line) for line in load_banner().rstrip("\n").split("\n"))


def render_banner(console: Console, *, enabled: bool = True) -> None:
    """在交互启动时渲染 Banner；终端过窄或非 tty 时降级为单行。"""
    if not enabled:
        return
    required = max(banner_width(), 2 + cell_len(TAGLINE))
    if not console.is_terminal or console.width < required:
        console.print(FALLBACK, style="bold cyan", markup=False, highlight=False, soft_wrap=True)
        return
    # soft_wrap 关闭 Rich 自动换行，保证 Art 布局不被破坏
    console.print(
        load_banner().rstrip("\n"),
        style="cyan",
        markup=False,
        highlight=False,
        soft_wrap=True,
    )
    console.print(f"  {TAGLINE}", style="bold cyan", markup=False, highlight=False, soft_wrap=True)


def render_startup(
    console: Console,
    *,
    version: str,
    provider: str,
    model: str,
    project: str,
    session: str,
) -> None:
    """渲染紧凑启动信息：身份栏 + 帮助入口；完整路径留给 `/status full`。"""
    identity = f"mini-pi {version} · {provider}/{model}"
    context = f"{project} · session {session}"
    hint = "/help for commands"
    console.print()
    # 身份栏是一个整体：两行都排得下才用。TERM=dumb 不报告真实宽度，固定走字段行
    can_use_bar = (
        console.is_terminal
        and not console.is_dumb_terminal
        and console.width >= cell_len(identity)
    )
    bar = style.spread(context, hint, width=console.width) if can_use_bar else None
    if bar is not None:
        console.print(identity, style="bold", markup=False, highlight=False, soft_wrap=True)
        console.print(bar, style=style.MUTED, markup=False, highlight=False, soft_wrap=True)
        console.print()
        return
    # 窄屏与非 tty 使用短字段行，避免依赖终端折行破坏信息顺序
    for line in (
        f"mini-pi {version}",
        f"model {provider}/{model}",
        f"project {project}",
        f"session {session}",
        hint,
    ):
        console.print(line, style=style.MUTED, markup=False, highlight=False, soft_wrap=True)
    console.print()
