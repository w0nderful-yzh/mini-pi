"""Workspace：路径边界控制与文件读写。"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from mini_pi.errors import WorkspaceViolationError


class Workspace:
    """所有文件操作的唯一入口，禁止 Tool 绕过此类直接 open()。"""

    def __init__(self, root: str | Path) -> None:
        """校验 root 为已存在目录并 resolve 为真实路径（拦截 /tmp 等符号链接）。"""
        candidate = Path(root).expanduser()
        if not candidate.is_dir():
            raise NotADirectoryError(f"workspace root is not a directory: {candidate}")
        # root 也做 resolve，保证后续比较基于真实路径（macOS /tmp 等场景）
        self._root = candidate.resolve()

    @property
    def root(self) -> Path:
        """返回已 resolve 的 workspace 根目录。"""
        return self._root

    def resolve(self, path: str | Path) -> Path:
        """解析并校验路径；任何逃逸都抛 WorkspaceViolationError（Fail Fast）。"""
        raw = Path(path).expanduser()
        candidate = raw if raw.is_absolute() else self._root / raw
        # resolve 会跟随 symlink，因此 symlink 逃逸同样会被下面的检查拦截
        resolved = candidate.resolve()
        if not resolved.is_relative_to(self._root):
            raise WorkspaceViolationError(f"path escapes workspace: {path!r}")
        return resolved

    def relative(self, path: str | Path) -> str:
        """返回相对 root 的 posix 路径，用于展示与 modified_files 记录。"""
        return self.resolve(path).relative_to(self._root).as_posix()

    def read_text(self, path: str | Path, *, encoding: str = "utf-8") -> str:
        """经 resolve 边界校验后读取文本。"""
        return self.resolve(path).read_text(encoding=encoding)

    def read_bytes(self, path: str | Path) -> bytes:
        """读取原始字节，供 read 工具做二进制嗅探与解码判断。"""
        return self.resolve(path).read_bytes()

    def entry_names(self, path: str | Path = ".") -> set[str]:
        """返回目录项的精确名称，避免大小写不敏感文件系统误匹配。"""
        return {entry.name for entry in self.resolve(path).iterdir()}

    def write_text(self, path: str | Path, content: str, *, encoding: str = "utf-8") -> Path:
        """原子写：先写同目录临时文件，再 os.replace 覆盖目标。"""
        target = self.resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding=encoding) as handle:
                handle.write(content)
            os.replace(tmp_name, target)
        except BaseException:
            # 失败时清理临时文件，避免留下半成品
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
            raise
        return target
