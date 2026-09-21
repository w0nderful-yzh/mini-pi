from __future__ import annotations

from pathlib import Path

from mini_pi.tools import build_default_registry
from mini_pi.workspace.workspace import Workspace


def test_default_registry_contains_phase1_tools(tmp_path: Path) -> None:
    """默认注册表按固定顺序包含第一阶段六个工具。"""
    registry = build_default_registry(Workspace(tmp_path))
    names = [schema.name for schema in registry.schemas()]
    assert names == [
        "read",
        "write",
        "edit",
        "search",
        "bash",
        "git_diff",
    ]
