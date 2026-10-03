"""显式安装 Pyright 后验证真实跨文件查询、修改、诊断和测试闭环。"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from mini_pi.config import ServerConfig, UserConfig
from mini_pi.interaction import ApprovalRequest, ApprovalResponse
from mini_pi.llm.types import ToolMessage
from mini_pi.security import SecurityPolicy
from mini_pi.session.runtime import AgentSession
from mini_pi.tools import build_default_registry
from mini_pi.workspace.workspace import Workspace
from tests.conftest import FakeLLMClient, assistant, tool_call
from tests.lsp.test_client import assert_dead


def test_real_pyright_query_edit_diagnostics_tests(tmp_path: Path) -> None:
    """固定版本真实 server，模型脚本仅控制调用顺序，断言实际协议和磁盘结果。"""
    binary = os.environ.get("MINI_PI_TEST_PYRIGHT")
    if binary is None:
        pytest.skip("set MINI_PI_TEST_PYRIGHT to explicitly installed pyright-langserver 1.1.414")
    command = Path(binary).resolve()
    version = subprocess.run(
        [str(command.with_name("index.js")), "--version"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert version.stdout.strip() == "pyright 1.1.414"
    (tmp_path / "lib.py").write_text(
        "def square(number: int) -> int:\n    return number * number\n"
    )
    (tmp_path / "app.py").write_text("from lib import square\nresult: str = square(3)\n")
    (tmp_path / "test_app.py").write_text(
        "import unittest\nfrom app import result\nclass TestApp(unittest.TestCase):\n    def test_result(self):\n        self.assertEqual(result, 9)\n"
    )
    position = {"path": "app.py", "line": 2, "column": 15}
    llm = FakeLLMClient(
        [
            assistant(
                tool_calls=[
                    tool_call("def", "lsp_definition", position),
                    tool_call("refs", "lsp_references", position),
                    tool_call("before", "lsp_diagnostics", {"path": "app.py"}),
                ]
            ),
            assistant(
                tool_calls=[
                    tool_call(
                        "edit",
                        "edit",
                        {
                            "path": "app.py",
                            "edits": [{"old_text": "result: str", "new_text": "result: int"}],
                        },
                    )
                ]
            ),
            assistant(tool_calls=[tool_call("after", "lsp_diagnostics", {"path": "app.py"})]),
            assistant(
                tool_calls=[
                    tool_call(
                        "test", "bash", {"command": f"{shlex.quote(sys.executable)} -m unittest -q"}
                    )
                ]
            ),
            assistant("done"),
        ]
    )
    approvals: list[ApprovalRequest] = []

    def approve(request: ApprovalRequest) -> ApprovalResponse:
        """显式批准本地 server 和测试命令，各自记录独立审批。"""
        approvals.append(request)
        return ApprovalResponse("allow")

    config = UserConfig(
        servers={"python": ServerConfig(kind="lsp", argv=(str(command), "--stdio"), timeout=20)}
    )
    registry = build_default_registry(
        Workspace(tmp_path), security=SecurityPolicy(cwd=tmp_path, config=config, approval=approve)
    )
    session = AgentSession.create(
        cwd=tmp_path,
        provider="deepseek",
        model="deepseek-flash",
        sessions_root=tmp_path / "sessions",
        registry=registry,
        llm=llm,
    )
    try:
        assert session.run("fix the type and run tests").stop_reason == "stop"
        observations = {
            message.tool_call_id: message
            for message in session.state.messages
            if isinstance(message, ToolMessage)
        }
        assert all(not message.is_error for message in observations.values())
        definitions = json.loads(observations["def"].content)["locations"]
        assert definitions[0]["path"] == "lib.py" and definitions[0]["range"]["start"] == {
            "line": 1,
            "column": 5,
        }
        assert {item["path"] for item in json.loads(observations["refs"].content)["locations"]} == {
            "app.py",
            "lib.py",
        }
        before, after = (json.loads(observations[key].content) for key in ("before", "after"))
        assert before["diagnostics"][0]["code"] == "reportAssignmentType"
        assert (
            after["diagnostics"] == []
            and after["version"] == 2
            and after["content_hash"] != before["content_hash"]
        )
        assert (
            "exit_code: 0" in observations["test"].content
            and "Ran 1 test" in observations["test"].content
        )
        pid = session._registry._lsp.pid
        # 不依赖 modified_files，外部改动后仍由同一进程重新分析当前磁盘。
        (tmp_path / "app.py").write_text("from lib import square\nresult: str = square(3)\n")
        external = json.loads(
            session._registry.execute("lsp_diagnostics", {"path": "app.py"}).content
        )
        assert (
            external["version"] == 3
            and external["diagnostics"]
            and session._registry._lsp.pid == pid
        )
        assert len(approvals) == 2
    finally:
        session.close()
    assert pid is not None
    assert_dead(pid)
