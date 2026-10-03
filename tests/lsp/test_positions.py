"""Unicode 字符边界和行尾位置不能被宽松转换掩盖。"""

import pytest

from mini_pi.lsp.positions import convert_range, from_wire, to_wire
from mini_pi.lsp.transport import LspError, LspProtocolError


@pytest.mark.parametrize("encoding,offset", [("utf-16", 4), ("utf-8", 10), ("utf-32", 3)])
def test_line_endings_and_round_trip(encoding: str, offset: int) -> None:
    """CRLF、CR、LF 与末尾空行使用相同的 Unicode 字符位置。"""
    text = "你好😀x\r\ny\rz\n"
    wire = to_wire(text, 1, 4, encoding)
    assert wire == {"line": 0, "character": offset}
    assert from_wire(text, wire, encoding) == {"line": 1, "column": 4}
    assert to_wire(text, 4, 1, encoding) == {"line": 3, "character": 0}
    assert from_wire(text, {"line": 0, "character": 999}, encoding)["column"] == 5


@pytest.mark.parametrize(
    "position",
    [
        {"line": -1, "character": 0},
        {"line": True, "character": 0},
        {"line": 0, "character": 1},
        {"line": 2, "character": 0},
    ],
)
def test_invalid_or_split_position_is_protocol_error(position: dict) -> None:
    """server 的非法行或 emoji 中间代码单元不能冒充正常位置。"""
    with pytest.raises(LspProtocolError):
        from_wire("😀\n", position, "utf-16")


def test_invalid_model_position_and_reversed_range() -> None:
    """越界模型参数和反向 server 范围均明确失败。"""
    with pytest.raises(LspError):
        to_wire("x", 1, 3, "utf-16")
    with pytest.raises(LspProtocolError):
        convert_range(
            "xy",
            {"start": {"line": 0, "character": 2}, "end": {"line": 0, "character": 1}},
            "utf-16",
        )
