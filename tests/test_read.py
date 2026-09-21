from __future__ import annotations

from pathlib import Path

import pytest

from mini_pi.errors import ToolError, WorkspaceViolationError
from mini_pi.tools.read import ReadTool
from mini_pi.workspace.workspace import Workspace


@pytest.fixture
def tool(tmp_path: Path) -> ReadTool:
    (tmp_path / "notes.txt").write_text("line1\nline2\nline3\n", encoding="utf-8")
    return ReadTool(Workspace(tmp_path))


def test_reads_whole_file(tool: ReadTool) -> None:
    """小文件一次读完，不出现续读提示。"""
    result = tool.execute(path="notes.txt")
    assert "line1\nline2\nline3" in result.content
    assert "Use offset=" not in result.content
    assert result.details == {"path": "notes.txt", "total_lines": 3}


def test_offset_and_limit(tool: ReadTool) -> None:
    """offset/limit 分页读取，并给出下一段起点。"""
    result = tool.execute(path="notes.txt", offset=2, limit=1)
    assert result.content.splitlines()[0] == "line2"
    assert "Use offset=3" in result.content


def test_offset_beyond_end_is_error(tool: ReadTool) -> None:
    """offset 越界属于调用错误，直接报错。"""
    with pytest.raises(ToolError, match="beyond end of file"):
        tool.execute(path="notes.txt", offset=99)


def test_missing_file_is_error(tool: ReadTool) -> None:
    with pytest.raises(ToolError, match="not a file"):
        tool.execute(path="missing.txt")


def test_escape_is_rejected(tool: ReadTool) -> None:
    """路径逃逸由 Workspace 拦截。"""
    with pytest.raises(WorkspaceViolationError):
        tool.execute(path="../secret.txt")


def test_binary_file_is_rejected(tmp_path: Path) -> None:
    """含 NUL 的文件按二进制处理，不把乱码塞给模型。"""
    (tmp_path / "bin.dat").write_bytes(b"\x00\x01\x02binary")
    tool = ReadTool(Workspace(tmp_path))
    with pytest.raises(ToolError, match="binary"):
        tool.execute(path="bin.dat")


def test_invalid_utf8_is_rejected(tmp_path: Path) -> None:
    """非 UTF-8 文本显式报错，而不是静默替换字符。"""
    (tmp_path / "bad.txt").write_bytes(b"\xff\xfe\x41\x42")
    tool = ReadTool(Workspace(tmp_path))
    with pytest.raises(ToolError):
        tool.execute(path="bad.txt")


def test_line_truncation_adds_hint(tmp_path: Path) -> None:
    """超过行数上限时截断并提示续读位置。"""
    (tmp_path / "long.txt").write_text("\n".join(f"l{i}" for i in range(5000)), encoding="utf-8")
    tool = ReadTool(Workspace(tmp_path))
    result = tool.execute(path="long.txt")
    assert "[Showing lines 1-2000 of 5000. Use offset=2001 to continue.]" in result.content
