from __future__ import annotations

from pathlib import Path

import pytest

from mini_pi.errors import WorkspaceViolationError
from mini_pi.tools.write import WriteTool
from mini_pi.workspace.workspace import Workspace


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    return Workspace(tmp_path)


def test_writes_new_file_and_creates_parents(workspace: Workspace) -> None:
    """新文件自动建父目录，并声明 modified_files。"""
    tool = WriteTool(workspace)
    result = tool.execute(path="pkg/mod.py", content="x = 1\n")
    assert (workspace.root / "pkg" / "mod.py").read_text(encoding="utf-8") == "x = 1\n"
    assert result.content == "Wrote 6 bytes to pkg/mod.py."
    assert result.details == {"path": "pkg/mod.py"}
    assert result.modified_files == ["pkg/mod.py"]


def test_overwrites_existing_file(workspace: Workspace) -> None:
    """整文件重写语义：旧内容被完全替换。"""
    target = workspace.root / "a.txt"
    target.write_text("old", encoding="utf-8")
    WriteTool(workspace).execute(path="a.txt", content="new")
    assert target.read_text(encoding="utf-8") == "new"


def test_byte_count_is_utf8(workspace: Workspace) -> None:
    """字节数按 UTF-8 编码计算，而不是字符数。"""
    result = WriteTool(workspace).execute(path="cn.txt", content="中文\n")
    assert result.content == "Wrote 7 bytes to cn.txt."


def test_escape_is_rejected(workspace: Workspace) -> None:
    """路径逃逸在写入前被拦截。"""
    with pytest.raises(WorkspaceViolationError):
        WriteTool(workspace).execute(path="../out.txt", content="x")
