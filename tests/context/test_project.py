"""项目级 AGENTS.md 发现与读取测试。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from mini_pi.context.project import ProjectInstruction, load_project_instructions
from mini_pi.errors import WorkspaceViolationError
from mini_pi.workspace.workspace import Workspace

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def init_repo(path: Path) -> None:
    """初始化最小 git 仓库，不依赖提交或全局用户配置。"""
    subprocess.run(
        ["git", "init", "--quiet"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )


def test_returns_empty_when_no_agents_file_exists(tmp_path: Path) -> None:
    """git 仓库内没有 AGENTS.md 时返回空列表。"""
    init_repo(tmp_path)

    assert load_project_instructions(Workspace(tmp_path)) == []


def test_loads_single_agents_file_with_workspace_relative_path(tmp_path: Path) -> None:
    """单文件返回内容及 workspace 相对来源路径。"""
    init_repo(tmp_path)
    (tmp_path / "AGENTS.md").write_text("root rules\n", encoding="utf-8")

    assert load_project_instructions(Workspace(tmp_path)) == [
        ProjectInstruction(path="AGENTS.md", content="root rules\n")
    ]


def test_loads_parent_before_child_from_git_root_to_workspace(tmp_path: Path) -> None:
    """嵌套 workspace 按 git root 到 workspace 的父子顺序加载。"""
    init_repo(tmp_path)
    workspace_root = tmp_path / "packages" / "app"
    workspace_root.mkdir(parents=True)
    (tmp_path / "AGENTS.md").write_text("root\n", encoding="utf-8")
    (tmp_path / "packages" / "AGENTS.md").write_text("packages\n", encoding="utf-8")
    (workspace_root / "AGENTS.md").write_text("app\n", encoding="utf-8")

    assert load_project_instructions(Workspace(workspace_root)) == [
        ProjectInstruction(path="../../AGENTS.md", content="root\n"),
        ProjectInstruction(path="../AGENTS.md", content="packages\n"),
        ProjectInstruction(path="AGENTS.md", content="app\n"),
    ]


def test_non_git_workspace_only_reads_workspace_root(tmp_path: Path) -> None:
    """非 git workspace 不向父目录继承规则。"""
    workspace_root = tmp_path / "plain"
    workspace_root.mkdir()
    (tmp_path / "AGENTS.md").write_text("parent\n", encoding="utf-8")
    (workspace_root / "AGENTS.md").write_text("local\n", encoding="utf-8")

    assert load_project_instructions(Workspace(workspace_root)) == [
        ProjectInstruction(path="AGENTS.md", content="local\n")
    ]


def test_ignores_non_exact_context_file_names(tmp_path: Path) -> None:
    """只识别精确文件名 AGENTS.md，不兼容其他名称或大小写。"""
    init_repo(tmp_path)
    (tmp_path / "agents.md").write_text("lowercase\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("claude\n", encoding="utf-8")

    assert load_project_instructions(Workspace(tmp_path)) == []


def test_rejects_agents_symlink_escaping_discovery_root(tmp_path: Path) -> None:
    """AGENTS.md symlink 指向 git 根外时必须触发 Workspace 边界错误。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    init_repo(repo)
    outside = tmp_path / "outside.md"
    outside.write_text("secret\n", encoding="utf-8")
    (repo / "AGENTS.md").symlink_to(outside)

    with pytest.raises(WorkspaceViolationError, match="escapes workspace"):
        load_project_instructions(Workspace(repo))


def test_rejects_agents_symlink_escaping_discovery_interval(tmp_path: Path) -> None:
    """即使目标仍在 git 根内，也不能跳到祖先链之外的兄弟目录。"""
    init_repo(tmp_path)
    workspace_root = tmp_path / "packages" / "app"
    workspace_root.mkdir(parents=True)
    sibling = tmp_path / "other"
    sibling.mkdir()
    target = sibling / "rules.md"
    target.write_text("sibling\n", encoding="utf-8")
    (workspace_root / "AGENTS.md").symlink_to(target)

    with pytest.raises(WorkspaceViolationError, match="discovery interval"):
        load_project_instructions(Workspace(workspace_root))


def test_invalid_utf8_fails_fast(tmp_path: Path) -> None:
    """AGENTS.md 不是 UTF-8 时直接暴露解码错误。"""
    init_repo(tmp_path)
    (tmp_path / "AGENTS.md").write_bytes(b"\xff\xfe")

    with pytest.raises(UnicodeDecodeError):
        load_project_instructions(Workspace(tmp_path))
