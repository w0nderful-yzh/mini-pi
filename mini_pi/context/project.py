"""发现并读取 workspace 生效的项目级 AGENTS.md。"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from mini_pi.errors import WorkspaceViolationError
from mini_pi.workspace.workspace import Workspace

_CONTEXT_FILE_NAME = "AGENTS.md"
_GIT_DISCOVERY_TIMEOUT_SECONDS = 5


@dataclass(frozen=True, slots=True)
class ProjectInstruction:
    """一份项目规则及其相对当前 workspace 的来源路径。"""

    path: str
    content: str


def load_project_instructions(workspace: Workspace) -> list[ProjectInstruction]:
    """按 git root 到 workspace 的父子顺序加载精确命名的 AGENTS.md。"""
    git_root = _find_git_root(workspace.root)
    discovery_root = git_root or workspace.root
    discovery_workspace = Workspace(discovery_root)
    directories = _directories_between(discovery_root, workspace.root)
    allowed_directories = set(directories)
    instructions: list[ProjectInstruction] = []

    for directory in directories:
        if _CONTEXT_FILE_NAME not in discovery_workspace.entry_names(directory):
            continue
        candidate = directory / _CONTEXT_FILE_NAME
        resolved = discovery_workspace.resolve(candidate)
        try:
            # symlink 即使仍落在 git 根内，也不能跳出 root → workspace 祖先链。
            if resolved.parent not in allowed_directories:
                raise WorkspaceViolationError(
                    f"AGENTS.md symlink escapes discovery interval: {candidate}"
                )
            content = discovery_workspace.read_text(resolved, encoding="utf-8")
        except FileNotFoundError:
            # 目录扫描后文件被并发删除时保持“未发现”语义。
            continue
        instructions.append(
            ProjectInstruction(
                path=_relative_to_workspace(candidate, workspace.root),
                content=content,
            )
        )

    return instructions


def _find_git_root(workspace_root: Path) -> Path | None:
    """使用 git 自身解析 worktree 根；非 git workspace 返回 None。"""
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=workspace_root,
        check=False,
        capture_output=True,
        text=True,
        timeout=_GIT_DISCOVERY_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        return None

    output = result.stdout.strip()
    if not output:
        raise RuntimeError("git rev-parse returned an empty repository root")
    git_root = Path(output).resolve()
    if not workspace_root.is_relative_to(git_root):
        raise WorkspaceViolationError(
            f"git root does not contain workspace: {git_root}"
        )
    return git_root


def _directories_between(root: Path, workspace_root: Path) -> tuple[Path, ...]:
    """生成 root 与 workspace 之间包含两端的父到子目录序列。"""
    relative_workspace = workspace_root.relative_to(root)
    directories = [root]
    current = root
    for part in relative_workspace.parts:
        current /= part
        directories.append(current)
    return tuple(directories)


def _relative_to_workspace(path: Path, workspace_root: Path) -> str:
    """返回可展示的 workspace 相对 POSIX 路径，允许父级规则包含 ..。"""
    return Path(os.path.relpath(path, start=workspace_root)).as_posix()
