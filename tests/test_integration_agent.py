from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from mini_pi.agent.agent import Agent
from mini_pi.auth import resolve_api_key
from mini_pi.cli.app import create_llm
from mini_pi.tools import build_default_registry
from mini_pi.workspace.workspace import Workspace

FIXTURE = Path(__file__).parent / "fixtures" / "sample_project"


def _credentials_available() -> bool:
    """环境变量或 /connect 保存的凭据任一存在即可运行。"""
    return resolve_api_key("openai", env_var="OPENAI_API_KEY") is not None or resolve_api_key(
        "deepseek", env_var="DEEPSEEK_API_KEY"
    ) is not None


# 真实 API 测试：默认被 addopts 排除，仅在显式 -m integration 且配置 Key 时运行
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _credentials_available(), reason="no LLM API key configured"),
]


def test_agent_fixes_failing_test(tmp_path: Path) -> None:
    """端到端闭环：Agent 自主定位失败用例、修改代码、再次验证通过。"""
    target = tmp_path / "sample_project"
    shutil.copytree(FIXTURE, target)
    workspace = Workspace(target)
    provider = os.environ.get("MINI_PI_PROVIDER", "openai")
    agent = Agent(
        llm=create_llm(provider, None),
        registry=build_default_registry(workspace),
        cwd=workspace.root,
        max_steps=30,
    )
    result = agent.run(
        "Run pytest, find the failing test, fix the code, then run pytest again to verify."
    )
    assert result.stop_reason in {"stop", "tool_calls"}
    # 用真实 pytest 复核 Agent 的修复结果，而不是只信模型总结
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=workspace.root,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "calculator.py" in agent.state.modified_files
