"""模型 Unicode 字符位置与 LSP 协商编码之间的严格转换。"""

from __future__ import annotations

import re
from typing import Any

from mini_pi.lsp.transport import LspError, LspProtocolError


def lines(text: str) -> list[str]:
    """保留最后的空行并统一 LF、CRLF 与 CR 的位置语义。"""
    return re.split(r"\r\n|\r|\n", text)


def units(text: str, encoding: str) -> int:
    """UTF-8 用字节、UTF-16 用代码单元、UTF-32 用 Unicode 字符计数。"""
    if encoding == "utf-8":
        return len(text.encode("utf-8"))
    if encoding == "utf-16":
        return len(text.encode("utf-16-le")) // 2
    if encoding == "utf-32":
        return len(text)
    raise LspProtocolError(f"unsupported LSP position encoding: {encoding}")


def to_wire(text: str, line: int, column: int, encoding: str) -> dict[str, int]:
    """模型位置为一基 Unicode 字符列，先验证范围再转为零基协议位置。"""
    source = lines(text)
    if not 1 <= line <= len(source) or not 1 <= column <= len(source[line - 1]) + 1:
        raise LspError("line/column is outside the file; positions are 1-based Unicode characters")
    return {"line": line - 1, "character": units(source[line - 1][: column - 1], encoding)}


def from_wire(text: str, position: Any, encoding: str) -> dict[str, int]:
    """把 server 位置转回字符列，超行尾按 LSP 规则夹到末尾但不拆字符。"""
    if not isinstance(position, dict):
        raise LspProtocolError("invalid LSP position")
    row, offset = position.get("line"), position.get("character")
    source = lines(text)
    if (
        type(row) is not int
        or type(offset) is not int
        or row < 0
        or offset < 0
        or row >= len(source)
    ):
        raise LspProtocolError("invalid LSP line/character")
    count = 0
    for column, char in enumerate(source[row]):
        if count == offset:
            return {"line": row + 1, "column": column + 1}
        count += units(char, encoding)
        if count > offset:
            raise LspProtocolError("LSP position splits a Unicode character")
    return {"line": row + 1, "column": len(source[row]) + 1}


def convert_range(text: str, value: Any, encoding: str) -> dict[str, dict[str, int]]:
    """验证范围并转换两端，拒绝反向或缺失的范围。"""
    if not isinstance(value, dict):
        raise LspProtocolError("invalid LSP range")
    start = from_wire(text, value.get("start"), encoding)
    end = from_wire(text, value.get("end"), encoding)
    if (end["line"], end["column"]) < (start["line"], start["column"]):
        raise LspProtocolError("reversed LSP range")
    return {"start": start, "end": end}
