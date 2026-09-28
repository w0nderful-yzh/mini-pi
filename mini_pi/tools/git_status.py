"""git_status 工具：观察工作区暂存/未暂存/未跟踪/冲突状态，不返回内容差异。"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

from mini_pi.errors import ToolError
from mini_pi.tools.base import Tool, ToolResult
from mini_pi.tools.process import run_process
from mini_pi.tools.truncate import truncate_text
from mini_pi.workspace.workspace import Workspace

# porcelain v1 的冲突状态：暂存侧与工作区侧同时有状态，既不算纯暂存也不算纯未暂存
_UNMERGED = frozenset({"DD", "AU", "UD", "UA", "DU", "AA", "UU"})

# 需要额外读取一个字段（原路径）的状态：重命名与复制
_PATH_PAIR = frozenset({"R", "C"})

# porcelain 用 NUL 分隔字段，路径因此不会被引号转义
_FIELD_SEP = "\x00"

_STATUS_TIMEOUT_S = 30


class GitStatusArgs(BaseModel):
    """无参数：状态查询固定覆盖整个 workspace。"""


@dataclass(frozen=True, slots=True)
class _StatusEntry:
    """一条 porcelain 记录；path 已转换成 workspace 相对路径。"""

    code: str
    path: str
    original_path: str | None = None

    @property
    def index_status(self) -> str:
        """暂存侧状态字符（X）。"""
        return self.code[0]

    @property
    def worktree_status(self) -> str:
        """工作区侧状态字符（Y）。"""
        return self.code[1]

    @property
    def is_untracked(self) -> bool:
        """是否未跟踪。"""
        return self.code == "??"

    @property
    def is_unmerged(self) -> bool:
        """是否处于合并冲突。"""
        return self.code in _UNMERGED

    @property
    def is_staged(self) -> bool:
        """暂存侧是否有改动（`?` / `!` 不是改动）。"""
        return not self.is_untracked and not self.is_unmerged and self.index_status not in " ?!"

    @property
    def is_unstaged(self) -> bool:
        """工作区侧是否有改动（`?` / `!` 不是改动）。"""
        return not self.is_untracked and not self.is_unmerged and self.worktree_status not in " ?!"

    def render(self) -> str:
        """按 git short 格式渲染一行；重命名/复制补出原路径。"""
        if self.original_path is None:
            return f"  {self.code} {self.path}"
        return f"  {self.code} {self.original_path} -> {self.path}"


class GitStatusTool(Tool):
    name = "git_status"
    description = (
        "Show the git working tree status of the workspace: branch plus staged, unstaged, "
        "untracked and conflicted paths. Read-only; use git_diff to inspect the content of "
        "those changes."
    )
    args_model = GitStatusArgs
    max_lines = 2000
    max_bytes = 50_000

    def __init__(self, workspace: Workspace) -> None:
        """持有 Workspace，git 固定在 workspace root 执行。"""
        self._workspace = workspace

    def execute(self) -> ToolResult:
        """观察工作区状态；非 git 工作区抛 ToolError，只读且不声明 modified_files。"""
        prefix = self._workspace_prefix()
        # -- 把查询限定在 workspace 内，避免把仓库其他目录的改动当成这个工作区的状态
        result = run_process(
            [
                "git",
                "status",
                "--porcelain=v1",
                "--branch",
                "--untracked-files=all",
                "--ignored=no",
                "-z",
                "--",
                ".",
            ],
            cwd=self._workspace.root,
            timeout_s=_STATUS_TIMEOUT_S,
            keep="head",
        )
        if result.exit_code != 0:
            raise ToolError(f"git status failed (exit {result.exit_code}): {result.stderr.strip()}")
        branch, entries = _parse_status(result.stdout, prefix)
        body, truncated = truncate_text(
            _render_status(branch, entries), max_lines=self.max_lines, max_bytes=self.max_bytes
        )
        if truncated:
            body += "\n[output truncated]"
        # 只读观察：details 供 UI 使用，modified_files 必须留空，状态不能变成“本工具改了什么”
        return ToolResult(
            content=body,
            details={
                "branch": branch,
                "paths": len(entries),
                "staged": sum(1 for entry in entries if entry.is_staged),
                "unstaged": sum(1 for entry in entries if entry.is_unstaged),
                "untracked": sum(1 for entry in entries if entry.is_untracked),
                "conflicted": sum(1 for entry in entries if entry.is_unmerged),
            },
        )

    def _workspace_prefix(self) -> str:
        """取 workspace 相对仓库根的路径前缀；非 git 工作区给出可解释错误。"""
        probe = run_process(
            ["git", "rev-parse", "--show-prefix"],
            cwd=self._workspace.root,
            timeout_s=_STATUS_TIMEOUT_S,
        )
        if probe.exit_code != 0:
            # 观察不到状态不能等同于“干净”：明确失败并带上 git 自己的原因
            detail = probe.stderr.strip() or f"exit {probe.exit_code}"
            raise ToolError(
                f"workspace is not inside a git worktree: {self._workspace.root} ({detail})"
            )
        return probe.stdout.strip()


def _parse_status(payload: str, prefix: str) -> tuple[str, list[_StatusEntry]]:
    """解析 `git status --porcelain=v1 -z`，返回分支说明与 workspace 相对路径条目。"""
    fields = payload.split(_FIELD_SEP)
    branch = ""
    index = 0
    if fields and fields[0].startswith("## "):
        branch = fields[0][3:].strip()
        index = 1
    entries: list[_StatusEntry] = []
    while index < len(fields):
        field = fields[index]
        index += 1
        # 末尾的 NUL 会产生空字段，直接跳过
        if not field:
            continue
        if len(field) < 4 or field[2] != " ":
            # porcelain v1 固定为「两位状态 + 空格 + 路径」；形状不符说明前提已被破坏
            raise ToolError(f"unexpected git status entry: {field!r}")
        code = field[:2]
        path = _workspace_path(field[3:], prefix)
        original: str | None = None
        if code[0] in _PATH_PAIR:
            # -z 模式下目标路径在前，原路径是紧随其后的独立字段
            if index >= len(fields) or not fields[index]:
                raise ToolError(f"git status rename entry has no source path: {field!r}")
            original = _workspace_path(fields[index], prefix)
            index += 1
        entries.append(_StatusEntry(code=code, path=path, original_path=original))
    return branch, entries


def _workspace_path(path: str, prefix: str) -> str:
    """把仓库根相对路径转成 workspace 相对路径。

    porcelain 的路径始终相对仓库根，而 workspace 可能只是仓库的子目录，
    因此按 `git rev-parse --show-prefix` 去前缀。查询已用 `-- .` 限定在
    workspace 内，前缀不匹配说明前提被破坏；此时宁可失败，也不把仓库其他
    目录的路径当成这个工作区的状态。
    """
    if not prefix:
        return path
    if path.startswith(prefix):
        return path[len(prefix) :]
    raise ToolError(f"git reported a path outside the workspace: {path!r}")


def _render_status(branch: str, entries: list[_StatusEntry]) -> str:
    """按类别渲染确定文本；没有状态时明确写 clean，避免被读成“观察失败”。"""
    lines = [f"branch: {branch}" if branch else "branch: (unknown)"]
    if not entries:
        lines.append("clean: no staged, unstaged, untracked or conflicted paths")
        return "\n".join(lines)
    groups = (
        # 冲突放最前：输出按 head 截断时，未解决的合并冲突不能被大量普通改动挤掉
        ("conflicted", [entry for entry in entries if entry.is_unmerged]),
        ("staged", [entry for entry in entries if entry.is_staged]),
        ("unstaged", [entry for entry in entries if entry.is_unstaged]),
        ("untracked", [entry for entry in entries if entry.is_untracked]),
    )
    for label, items in groups:
        if not items:
            continue
        lines.append(f"{label} ({len(items)}):")
        # 按路径排序，使同一工作区在任意 git 版本下输出一致
        lines.extend(entry.render() for entry in sorted(items, key=lambda item: item.path))
    return "\n".join(lines)
