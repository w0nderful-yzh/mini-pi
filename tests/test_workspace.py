from __future__ import annotations

from pathlib import Path

import pytest

from mini_pi.errors import WorkspaceViolationError
from mini_pi.workspace.workspace import Workspace


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
    return Workspace(tmp_path)


def test_resolve_relative_path(workspace: Workspace) -> None:
    """相对路径基于 workspace root 解析。"""
    assert workspace.resolve("src/app.py") == workspace.root / "src" / "app.py"


def test_resolve_dot_is_root(workspace: Workspace) -> None:
    """`.` 解析为 workspace root 自身。"""
    assert workspace.resolve(".") == workspace.root


def test_resolve_traversal_inside_is_allowed(workspace: Workspace) -> None:
    """仍在 root 内的 ../ 往返是合法的，不做过度拦截。"""
    assert workspace.resolve("src/../src/app.py") == workspace.root / "src" / "app.py"


def test_absolute_path_inside_is_allowed(workspace: Workspace) -> None:
    """root 内的绝对路径允许使用。"""
    assert workspace.resolve(workspace.root / "src" / "app.py").name == "app.py"


def test_parent_escape_is_rejected(workspace: Workspace) -> None:
    """../ 逃逸直接报错，不自动修正。"""
    with pytest.raises(WorkspaceViolationError, match="escapes workspace"):
        workspace.resolve("../secret.txt")


def test_absolute_escape_is_rejected(workspace: Workspace) -> None:
    """root 外绝对路径直接报错。"""
    with pytest.raises(WorkspaceViolationError, match="escapes workspace"):
        workspace.resolve("/etc/passwd")


def test_symlink_file_escape_is_rejected(workspace: Workspace) -> None:
    """指向外部的 symlink 文件在 resolve（跟随链接）后被拦截。"""
    outside = workspace.root.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (workspace.root / "link.txt").symlink_to(outside)
    with pytest.raises(WorkspaceViolationError):
        workspace.read_text("link.txt")


def test_symlink_dir_escape_is_rejected(workspace: Workspace) -> None:
    """通过 symlink 目录访问外部文件同样被拦截。"""
    outside = workspace.root.parent / "outside_dir"
    outside.mkdir()
    (outside / "data.txt").write_text("secret", encoding="utf-8")
    (workspace.root / "linkdir").symlink_to(outside, target_is_directory=True)
    with pytest.raises(WorkspaceViolationError):
        workspace.read_text("linkdir/data.txt")


def test_read_text(workspace: Workspace) -> None:
    """read_text 统一走 resolve 边界。"""
    assert workspace.read_text("src/app.py") == "print('hi')\n"


def test_write_text_is_atomic_and_creates_parents(workspace: Workspace) -> None:
    """自动创建父目录，写完后不残留临时文件。"""
    target = workspace.write_text("pkg/sub/new.txt", "hello")
    assert target.read_text(encoding="utf-8") == "hello"
    leftovers = list((workspace.root / "pkg" / "sub").glob(".*tmp"))
    assert leftovers == []


def test_relative_uses_posix_separators(workspace: Workspace) -> None:
    """对外展示的路径统一使用 posix 分隔符。"""
    assert workspace.relative("src/app.py") == "src/app.py"


def test_root_must_exist(tmp_path: Path) -> None:
    """workspace root 必须已存在，否则构造失败。"""
    with pytest.raises(NotADirectoryError):
        Workspace(tmp_path / "missing")


def test_write_through_symlinked_parent_is_rejected(workspace: Workspace) -> None:
    """写入路径的父目录是外指 symlink 时同样拦截。"""
    # 注意：tmp_path.parent 被同会话测试共享，目录名必须唯一避免互相污染
    outside = workspace.root.parent / "outside_write_dir"
    outside.mkdir()
    (workspace.root / "linkdir").symlink_to(outside, target_is_directory=True)
    with pytest.raises(WorkspaceViolationError):
        workspace.write_text("linkdir/new.txt", "x")


def test_expanduser_is_not_a_shortcut(workspace: Workspace) -> None:
    """`~` 展开后按绝对路径校验，不允许绕过边界。"""
    with pytest.raises(WorkspaceViolationError):
        workspace.resolve("~/secret.txt")
