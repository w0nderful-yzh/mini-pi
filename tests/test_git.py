from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from mini_pi.errors import ToolError
from mini_pi.tools.git import GitDiffTool
from mini_pi.workspace.workspace import Workspace

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def git(cwd: Path, *args: str) -> None:
    """在测试仓库执行 git 命令，显式指定提交身份避免依赖全局配置。"""
    subprocess.run(
        ["git", "-c", "user.email=test@example.com", "-c", "user.name=test", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init")
    (tmp_path / "a.txt").write_text("old\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("old-b\n", encoding="utf-8")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "init")
    return tmp_path


def test_unstaged_diff(repo: Path) -> None:
    """默认展示工作区未暂存改动。"""
    (repo / "a.txt").write_text("new\n", encoding="utf-8")
    tool = GitDiffTool(Workspace(repo))
    result = tool.execute()
    assert "-old" in result.content
    assert "+new" in result.content


def test_staged_diff(repo: Path) -> None:
    """staged=True 时展示已暂存改动。"""
    (repo / "a.txt").write_text("new\n", encoding="utf-8")
    git(repo, "add", "a.txt")
    tool = GitDiffTool(Workspace(repo))
    result = tool.execute(staged=True)
    assert "+new" in result.content


def test_no_changes(repo: Path) -> None:
    """无改动时返回明确文本而不是空内容。"""
    tool = GitDiffTool(Workspace(repo))
    assert tool.execute().content == "No changes."


def test_path_filter(repo: Path) -> None:
    """路径过滤只返回目标文件的 diff。"""
    (repo / "a.txt").write_text("new-a\n", encoding="utf-8")
    (repo / "b.txt").write_text("new-b\n", encoding="utf-8")
    tool = GitDiffTool(Workspace(repo))
    result = tool.execute(path="b.txt")
    assert "new-b" in result.content
    assert "new-a" not in result.content


def test_not_a_repo_is_tool_error(tmp_path: Path) -> None:
    """非 git 仓库属于可预期失败，抛 ToolError 让模型纠正。"""
    tool = GitDiffTool(Workspace(tmp_path))
    with pytest.raises(ToolError, match="git diff failed"):
        tool.execute()
