from __future__ import annotations

import pytest

from mini_pi.tools.truncate import truncate_text


def test_head_lines() -> None:
    """默认保留头部：行数超限时截断尾部。"""
    text, truncated = truncate_text(
        "\n".join(str(i) for i in range(10)), max_lines=3, max_bytes=10_000
    )
    assert text == "0\n1\n2"
    assert truncated is True


def test_tail_lines() -> None:
    """keep=tail 时保留最近的输出行。"""
    text, truncated = truncate_text(
        "\n".join(str(i) for i in range(10)), max_lines=3, max_bytes=10_000, keep="tail"
    )
    assert text == "7\n8\n9"
    assert truncated is True


def test_head_bytes() -> None:
    """单行超字节上限时截为空串，由调用方决定提示文案。"""
    text, truncated = truncate_text("a" * 100, max_lines=10, max_bytes=10)
    assert len(text) <= 9
    assert truncated is True


def test_tail_bytes() -> None:
    """按整行丢弃，保留末尾能放下的行。"""
    lines = ["x" * 20, "y" * 20, "z" * 20]
    text, truncated = truncate_text("\n".join(lines), max_lines=10, max_bytes=40, keep="tail")
    assert text == "z" * 20
    assert truncated is True


def test_no_truncation() -> None:
    """未超限时原样返回且标记为未截断。"""
    text, truncated = truncate_text("short", max_lines=10, max_bytes=100)
    assert text == "short"
    assert truncated is False


def test_invalid_limits() -> None:
    """非法上限属于编程错误，直接报错。"""
    with pytest.raises(ValueError, match="limits"):
        truncate_text("x", max_lines=0, max_bytes=10)
