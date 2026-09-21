from __future__ import annotations

import sys
from pathlib import Path

import pytest

from mini_pi.tools.bash import BashTool
from mini_pi.workspace.workspace import Workspace


@pytest.fixture
def tool(tmp_path: Path) -> BashTool:
    return BashTool(Workspace(tmp_path))


def test_successful_command(tool: BashTool) -> None:
    """成功命令返回 exit_code 0 与 stdout。"""
    result = tool.execute(command="echo hello")
    assert "exit_code: 0" in result.content
    assert "hello" in result.content
    assert result.details is not None
    assert result.details["exit_code"] == 0


def test_non_zero_exit_is_a_normal_result(tool: BashTool) -> None:
    """非 0 退出码是正常 Observation，不抛 ToolError。"""
    result = tool.execute(command="exit 3")
    assert "exit_code: 3" in result.content


def test_stderr_is_preserved(tool: BashTool) -> None:
    """stderr 必须原样带回，不能被吞掉。"""
    result = tool.execute(command="echo oops 1>&2")
    assert "oops" in result.content
    assert "stderr:" in result.content


def test_cwd_is_workspace_root(tool: BashTool, tmp_path: Path) -> None:
    """命令在 workspace root 下执行。"""
    tool.execute(command="pwd > cwd.txt")
    recorded = Path((tmp_path / "cwd.txt").read_text(encoding="utf-8").strip()).resolve()
    assert recorded == tmp_path.resolve()


def test_timeout_is_reported(tool: BashTool) -> None:
    """超时如实返回并标记 timed_out。"""
    command = f'"{sys.executable}" -c "import time; time.sleep(10)"'
    result = tool.execute(command=command, timeout=1)
    assert "timed out" in result.content
    assert result.details is not None
    assert result.details["timed_out"] is True


def test_invalid_timeout_is_rejected_by_schema(tool: BashTool) -> None:
    """timeout 范围由参数模型约束（1..600）。"""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        BashTool.args_model.model_validate({"command": "echo hi", "timeout": 0})
