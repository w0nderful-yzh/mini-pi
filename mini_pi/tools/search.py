"""search 工具：优先 ripgrep，无 rg 时用内置 Python 扫描。"""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
from collections.abc import Iterator
from pathlib import Path

from pydantic import BaseModel, Field

from mini_pi.errors import ToolArgumentError, ToolError
from mini_pi.tools.base import Tool, ToolResult
from mini_pi.tools.process import run_process
from mini_pi.workspace.workspace import Workspace


class SearchArgs(BaseModel):
    pattern: str = Field(description="Literal text or regular expression to search for.")
    path: str = Field(default=".", description="File or directory to search, relative to the workspace root.")
    glob: str | None = Field(default=None, description="Only search files matching this glob, e.g. '*.py'.")
    is_regex: bool = Field(default=False, description="Treat pattern as a regular expression.")
    limit: int = Field(default=100, ge=1, le=1000, description="Maximum number of matching lines to return.")


class SearchTool(Tool):
    name = "search"
    description = "Search text or regex across workspace files and return file:line:text matches. Skips .git/.venv/node_modules and binary files."
    args_model = SearchArgs
    skip_dirs = frozenset(
        {
            ".git",
            ".venv",
            "node_modules",
            "__pycache__",
            ".mypy_cache",
            ".pytest_cache",
            ".ruff_cache",
            ".idea",
        }
    )
    max_line_chars = 500
    max_file_bytes = 1_000_000

    def __init__(self, workspace: Workspace) -> None:
        self._workspace = workspace

    def execute(
        self,
        pattern: str,
        path: str = ".",
        glob: str | None = None,
        is_regex: bool = False,
        limit: int = 100,
    ) -> ToolResult:
        if not pattern:
            raise ToolArgumentError("pattern must not be empty")
        if is_regex:
            # 提前编译，正则语法错误属于参数错误而非工具故障
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ToolArgumentError(f"invalid regex pattern: {exc}") from exc
        base = self._workspace.resolve(path)
        if not base.exists():
            raise ToolError(f"path not found: {self._workspace.relative(path)}")
        rg = shutil.which("rg")
        if rg is not None and base.is_dir():
            engine = "rg"
            matches, truncated = self._search_rg(rg, pattern, base, glob, is_regex, limit)
        else:
            engine = "python"
            matches, truncated = self._search_python(pattern, base, glob, is_regex, limit)
        if not matches:
            return ToolResult(content="No matches found", details={"engine": engine, "count": 0})
        body = "\n".join(matches)
        if truncated:
            body += f"\n[Truncated at {limit} matches. Narrow the search or raise limit.]"
        return ToolResult(content=body, details={"engine": engine, "count": len(matches)})

    def _search_rg(
        self,
        rg: str,
        pattern: str,
        base: Path,
        glob: str | None,
        is_regex: bool,
        limit: int,
    ) -> tuple[list[str], bool]:
        argv = [rg, "--line-number", "--no-heading", "--color", "never"]
        if not is_regex:
            argv.append("--fixed-strings")
        if glob:
            argv.extend(["--glob", glob])
        argv.extend(["--", pattern, str(base)])
        # rg 的匹配通常在输出开头，按 head 保留避免被大仓库输出冲掉
        result = run_process(argv, cwd=self._workspace.root, timeout_s=60, keep="head")
        if result.exit_code not in (0, 1):
            raise ToolError(f"rg failed (exit {result.exit_code}): {result.stderr.strip()}")
        matches: list[str] = []
        truncated = False
        for line in result.stdout.splitlines():
            if len(matches) >= limit:
                truncated = True
                break
            parts = line.split(":", 2)
            if len(parts) != 3:
                raise ToolError(f"unexpected rg output line: {line!r}")
            file_part, line_no, text = parts
            rel = Path(file_part).resolve().relative_to(self._workspace.root).as_posix()
            matches.append(f"{rel}:{line_no}:{text[: self.max_line_chars]}")
        return matches, truncated

    def _search_python(
        self,
        pattern: str,
        base: Path,
        glob: str | None,
        is_regex: bool,
        limit: int,
    ) -> tuple[list[str], bool]:
        files = [base] if base.is_file() else list(self._iter_files(base))
        matches: list[str] = []
        truncated = False
        for file_path in files:
            if len(matches) >= limit:
                truncated = True
                break
            rel = self._workspace.relative(file_path)
            if glob and not (
                fnmatch.fnmatch(file_path.name, glob) or fnmatch.fnmatch(rel, glob)
            ):
                continue
            try:
                if file_path.stat().st_size > self.max_file_bytes:
                    continue
                data = self._workspace.read_bytes(file_path)
            except OSError:
                continue
            # 二进制文件直接跳过，避免乱码行命中
            if b"\x00" in data[:8192]:
                continue
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                continue
            for line_no, line in enumerate(text.splitlines(), start=1):
                if len(matches) >= limit:
                    truncated = True
                    break
                hit = re.search(pattern, line) if is_regex else pattern in line
                if hit:
                    matches.append(f"{rel}:{line_no}:{line[: self.max_line_chars]}")
        return matches, truncated

    def _iter_files(self, root: Path) -> Iterator[Path]:
        """确定性遍历（目录/文件名排序），跳过忽略目录。"""
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(name for name in dirnames if name not in self.skip_dirs)
            for name in sorted(filenames):
                yield Path(dirpath) / name
