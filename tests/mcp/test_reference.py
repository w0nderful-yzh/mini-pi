"""显式安装固定官方 filesystem server 后验证真实 stdio 工具闭环。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from mini_pi.config import ServerConfig, UserConfig
from mini_pi.errors import ToolError
from mini_pi.interaction import ApprovalRequest, ApprovalResponse
from mini_pi.mcp.catalog import tool_alias
from mini_pi.security import SecurityPolicy
from mini_pi.session.models import ToolCatalogEntry
from mini_pi.session.runtime import AgentSession
from mini_pi.tools import build_default_registry
from mini_pi.workspace.workspace import Workspace
from tests.conftest import FakeLLMClient, assistant
from tests.lsp.test_client import assert_dead


def test_official_filesystem_server_read_write_and_approval(tmp_path: Path) -> None:
    """固定 server 真实读写，拒绝零副作用、允许一次执行、连接复用并正常回收。"""
    configured = os.environ.get("MINI_PI_TEST_MCP_FILESYSTEM")
    if configured is None:
        pytest.skip(
            "set MINI_PI_TEST_MCP_FILESYSTEM to explicitly installed filesystem server 2026.8.31"
        )
    binary = Path(configured).resolve()
    assert json.loads((binary.parents[1] / "package.json").read_text())["version"] == "2026.8.31"
    (tmp_path / "hello.txt").write_text("hello MCP\n")
    approvals: list[ApprovalRequest] = []
    decisions = iter(["allow", "deny", "allow"])

    def approve(request: ApprovalRequest) -> ApprovalResponse:
        """本次启动允许，首次写工具拒绝，第二次写工具显式允许。"""
        approvals.append(request)
        return ApprovalResponse(next(decisions))

    config = UserConfig(
        servers={
            "files": ServerConfig(
                kind="mcp",
                argv=(str(binary), str(tmp_path)),
                tools=("read_file", "list_directory", "write_file"),
                readonly_tools=("read_file", "list_directory"),
                timeout=20,
            )
        }
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
        llm=FakeLLMClient([assistant("ready")]),
    )
    try:
        session.run("initialize tools")
        active = session._registry
        pid = active._mcp.clients["files"].pid
        read = json.loads(
            active.execute(
                tool_alias("files", "read_file"), {"path": str(tmp_path / "hello.txt")}
            ).content
        )
        listing = json.loads(
            active.execute(tool_alias("files", "list_directory"), {"path": str(tmp_path)}).content
        )
        assert read["text"] == "hello MCP\n" and "hello.txt" in listing["text"]
        target = tmp_path / "written.txt"
        parameters = {"path": str(target), "content": "approved MCP write\n"}
        with pytest.raises(ToolError):
            active.execute(tool_alias("files", "write_file"), parameters)
        assert not target.exists()
        result = active.execute(tool_alias("files", "write_file"), parameters)
        assert target.read_text() == "approved MCP write\n" and result.modified_files == []
        assert active._mcp.clients["files"].pid == pid and len(approvals) == 3
        assert any(isinstance(entry, ToolCatalogEntry) for entry in session._session.entries)
    finally:
        session.close()
    assert_dead(pid)
