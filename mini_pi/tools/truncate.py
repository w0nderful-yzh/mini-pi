"""输出截断：行数与字节双限，按整行保留。"""

from __future__ import annotations

from typing import Literal


def truncate_text(
    text: str, *, max_lines: int, max_bytes: int, keep: Literal["head", "tail"] = "head"
) -> tuple[str, bool]:
    """返回 (截断后文本, 是否截断)；按整行丢弃，不切半行。

    keep="head" 用于 read/search（保留开头），"tail" 用于 bash（保留最近输出）。
    """
    if max_lines <= 0 or max_bytes <= 0:
        raise ValueError("limits must be > 0")
    lines = text.splitlines()
    truncated = False
    if len(lines) > max_lines:
        lines = lines[:max_lines] if keep == "head" else lines[-max_lines:]
        truncated = True

    def encoded_length(candidate: list[str]) -> int:
        return len("\n".join(candidate).encode("utf-8"))

    # 逐行丢弃直到满足字节上限；UTF-8 多字节字符不会被切断
    while lines and encoded_length(lines) > max_bytes:
        lines.pop() if keep == "head" else lines.pop(0)
        truncated = True
    return "\n".join(lines), truncated
