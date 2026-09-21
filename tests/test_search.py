from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from mini_pi.errors import ToolArgumentError
from mini_pi.tools.search import SearchTool, _find_rg
from mini_pi.workspace.workspace import Workspace


@pytest.fixture
def tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SearchTool:
    # 强制走 Python 引擎，保证无 rg 环境下测试确定性
    monkeypatch.setattr(shutil, "which", lambda *args, **kwargs: None)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (tmp_path / "src" / "other.py").write_text("value = 42\n", encoding="utf-8")
    (tmp_path / "notes.md").write_text("add documentation here\n", encoding="utf-8")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "ignored.py").write_text("add ignored\n", encoding="utf-8")
    return SearchTool(Workspace(tmp_path))


def test_literal_search(tool: SearchTool) -> None:
    """字面量搜索返回 file:line:text，并跳过 .venv 等目录。"""
    result = tool.execute(pattern="add")
    lines = result.content.splitlines()
    assert "src/app.py:1:def add(a, b):" in lines
    assert "notes.md:1:add documentation here" in lines
    assert all(".venv" not in line for line in lines)
    assert result.details is not None
    assert result.details["engine"] == "python"


def test_regex_search(tool: SearchTool) -> None:
    result = tool.execute(pattern=r"return a \+ b", is_regex=True)
    assert "src/app.py:2:    return a + b" in result.content


def test_glob_filter(tool: SearchTool) -> None:
    result = tool.execute(pattern="add", glob="*.py")
    assert "notes.md" not in result.content


def test_no_matches(tool: SearchTool) -> None:
    result = tool.execute(pattern="does-not-exist")
    assert result.content == "No matches found"


def test_limit_adds_truncation_hint(tool: SearchTool) -> None:
    result = tool.execute(pattern="add", limit=1)
    assert "[Truncated at 1 matches" in result.content


def test_invalid_regex_is_argument_error(tool: SearchTool) -> None:
    with pytest.raises(ToolArgumentError, match="invalid regex"):
        tool.execute(pattern="[", is_regex=True)


def test_search_single_file(tool: SearchTool) -> None:
    result = tool.execute(pattern="add", path="src/app.py")
    assert "src/app.py:1" in result.content
    assert "notes.md" not in result.content


def test_binary_files_are_skipped(tool: SearchTool, tmp_path: Path) -> None:
    """含 NUL 的文件不得把乱码或伪匹配带回结果。"""
    (tmp_path / "blob.bin").write_bytes(b"\x00add\x00")
    result = tool.execute(pattern="add")
    assert "blob.bin" not in result.content


def test_binary_content_in_text_extension_is_skipped(tool: SearchTool, tmp_path: Path) -> None:
    """扩展名是文本但内容含 NUL 时也按二进制跳过。"""
    (tmp_path / "fake.txt").write_bytes(b"\xff\xfe\x00add")
    result = tool.execute(pattern="add")
    assert "fake.txt" not in result.content


@pytest.mark.skipif(_find_rg() is None, reason="ripgrep not installed")
def test_rg_engine(tmp_path: Path) -> None:
    """有 rg 时走 rg 引擎，输出仍归一为 workspace 相对路径。"""
    (tmp_path / "a.py").write_text("needle\n", encoding="utf-8")
    tool = SearchTool(Workspace(tmp_path))
    result = tool.execute(pattern="needle")
    assert result.details is not None
    assert result.details["engine"] == "rg"
    assert "a.py:1:needle" in result.content


def test_find_rg_prefers_environment_binary(tmp_path: Path) -> None:
    """优先使用当前环境 bin 下的 rg（ripgrep-bin），不依赖 PATH。"""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    rg_path = fake_bin / "rg"
    rg_path.write_text("#!/bin/sh\n", encoding="utf-8")
    rg_path.chmod(0o755)
    assert _find_rg(executable=str(fake_bin / "python")) == str(rg_path)
