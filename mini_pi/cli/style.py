"""CLI 视觉词汇：语义颜色、行首标记与标签对齐。

console / banner / status 三个展示模块共用同一套词汇，避免同一状态在不同界面用不同
颜色或符号。颜色只作辅助：每个状态都同时有文字或符号承载，NO_COLOR 与非 tty 下仍可读。
"""

from __future__ import annotations

from rich.cells import cell_len

# 语义颜色：进行中 / 成功 / 注意 / 失败
RUNNING = "cyan"
SUCCESS = "green"
WARNING = "yellow"
FAILURE = "red"
# 次要信息（统计、路径、提示）统一 dim，不与状态色抢注意力
MUTED = "dim"

# 行首标记：与颜色成对出现
MARK_USER = "›"
MARK_RUNNING = "●"
MARK_OK = "✓"
MARK_FAILED = "✗"
MARK_WARNING = "⚠"

# 思考指示：单行、单格宽度的帧序列。这两点都是硬要求——只要渲染高度恒为 1 行、
# 帧宽恒定，Live 区域清除时就只需回退一行，不会因为裁剪或折行留下残影。
THINKING_FRAMES = ("◜", "◝", "◞", "◟")
THINKING_LABEL = "Thinking…"
THINKING_FPS = 8.0

# "Label  value" 行的标签列宽：所有命令共用，保证跨命令数值对齐
LABEL_WIDTH = 16


def row(label: str, value: str) -> str:
    """把一行标签值对齐到统一列宽；标签过长时至少留一个空格。"""
    return f"{label}{' ' * max(1, LABEL_WIDTH - cell_len(label))}{value}"


def hanging(value: str) -> str:
    """续行：与 row 的值列对齐，用于同一标签下的第二行说明。"""
    return f"{' ' * LABEL_WIDTH}{value}"


def spread(left: str, right: str, *, width: int) -> str | None:
    """把左右两段排在同一行；放不下时返回 None，由调用方换行降级。"""
    gap = width - cell_len(left) - cell_len(right)
    if gap < 2:
        return None
    return f"{left}{' ' * gap}{right}"


def count(value: int) -> str:
    """计数统一加千位分隔，便于一眼读出量级。"""
    return f"{value:,}"


def thinking_line(elapsed: float) -> str:
    """按经过秒数取帧，返回固定宽度的一行思考指示。"""
    frame = THINKING_FRAMES[int(elapsed * THINKING_FPS) % len(THINKING_FRAMES)]
    return f"{frame} {THINKING_LABEL}"
