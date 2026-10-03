"""MCP Session 目录、恢复、压缩与 CLI 生命周期的离线真实进程验证。"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mini_pi.cli.app import app
from mini_pi.cli.console import ConsoleRenderer
from mini_pi.cli.interaction import create_approval_handler
from mini_pi.config import UserConfig
from mini_pi.context.compaction import prepare_compaction
from mini_pi.errors import SessionError
from mini_pi.interaction import ApprovalRequest, ApprovalResponse
from mini_pi.llm.types import SystemMessage, ToolMessage
from mini_pi.mcp.catalog import tool_alias
from mini_pi.security import ApprovalHandler, SecurityPolicy
from mini_pi.session.jsonl import JsonlSession
from mini_pi.session.models import ControlEntry, ToolCatalogEntry
from mini_pi.session.runtime import AgentSession
from mini_pi.tools import build_default_registry
from mini_pi.workspace.workspace import Workspace
from tests.conftest import FakeLLMClient, assistant, tool_call
from tests.lsp.test_client import assert_dead
from tests.mcp.test_client import config


def test_mcp_can_be_imported_before_tools() -> None:
    """独立 SDK 使用者可先导入目录模块，不依赖 pytest 已加载工具包的顺序。"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from mini_pi.mcp.catalog import MCPCatalog; from mini_pi.tools import build_default_registry",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_cli_reload_redacts_new_secrets_before_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """新环境引用在 reload 启动审批前登记，同时保留历史凭据的屏蔽。"""
    old_secret, new_secret = "old-credential-123", "new-credential-456"
    monkeypatch.setenv("OLD_CREDENTIAL", old_secret)
    monkeypatch.setenv("NEW_CREDENTIAL", new_secret)
    old = config(tmp_path)
    old = UserConfig(
        servers={
            "local": old.servers["local"].model_copy(
                update={"env": {"TEST_SECRET": "OLD_CREDENTIAL"}}
            )
        }
    )
    new = UserConfig(
        servers={
            "local": old.servers["local"].model_copy(
                update={
                    "env": {"TEST_SECRET": "NEW_CREDENTIAL"},
                    "argv": (*old.servers["local"].argv, new_secret),
                }
            )
        }
    )
    configurations = iter([old, new])
    monkeypatch.setattr("mini_pi.cli.app.load_config", lambda: next(configurations))
    monkeypatch.setattr("mini_pi.cli.app.create_llm", lambda *args, **kwargs: FakeLLMClient([]))
    monkeypatch.setattr("mini_pi.cli.app.load_last_connection", lambda: None)
    monkeypatch.setattr(
        "mini_pi.cli.app.save_last_connection", lambda *args: tmp_path / "auth.json"
    )
    monkeypatch.setattr("mini_pi.session.jsonl._sessions_root", lambda path: tmp_path / "sessions")
    approvals: list[str] = []
    monkeypatch.setattr("mini_pi.cli.interaction._interactive_terminal", lambda renderer: True)

    def handler(renderer: ConsoleRenderer) -> ApprovalHandler:
        """实际 startup 回调先检查脱敏表已包含新旧值。"""
        production_approval = create_approval_handler(renderer)

        def approve(request: ApprovalRequest) -> ApprovalResponse:
            """批准固定测试 server，不能用事后渲染证明先前没有泄漏。"""
            assert renderer.redact(old_secret) == "[REDACTED]"
            assert renderer.redact(new_secret) == "[REDACTED]"
            approvals.append(request.target)
            return production_approval(request)

        return approve

    monkeypatch.setattr("mini_pi.cli.app.create_approval_handler", handler)
    result = CliRunner().invoke(
        app, ["--cwd", str(tmp_path), "--no-banner"], input="/mcp reload\n1\n/tools\n/exit\n"
    )
    assert result.exit_code == 0 and approvals == ["local"], result.output
    assert old_secret not in result.output and new_secret not in result.output
    assert "[REDACTED]" in result.output


def runtime(tmp_path: Path, mode: str = "normal") -> AgentSession:
    """生产 Session 装配加离线模型，MCP 审批与目录必须实际落盘。"""
    alias = tool_alias("local", "calc.add")
    return AgentSession.create(
        cwd=tmp_path,
        provider="deepseek",
        model="deepseek-flash",
        sessions_root=tmp_path / "sessions",
        llm=FakeLLMClient(
            [assistant(tool_calls=[tool_call("call1", alias, {"value": 3})]), assistant("done")]
        ),
        registry=build_default_registry(
            Workspace(tmp_path),
            security=SecurityPolicy(
                cwd=tmp_path,
                config=config(tmp_path, mode),
                approval=lambda request: ApprovalResponse("allow"),
            ),
        ),
    )


def test_durable_catalog_projection_compaction_and_resume_disabled(tmp_path: Path) -> None:
    """目录先于消息且不进入摘要，禁用后恢复旧结果不重放或启动已移除 server。"""
    session = runtime(tmp_path)
    try:
        assert session.run("calculate").stop_reason == "stop"
        loaded = JsonlSession.load(session.path)
        catalog = next(item for item in loaded.entries if isinstance(item, ToolCatalogEntry))
        assert isinstance(loaded.entries[0], ControlEntry) and loaded.entries[1] == catalog
        assert len(catalog.tools) == 2 and catalog.tools[0].alias == tool_alias("local", "calc.add")
        assert loaded.replay().messages == tuple(session.state.messages)
        plan = prepare_compaction(loaded.active_entries(), keep_recent_tokens=5).plan
        assert plan is not None and all(
            "tool_catalog" not in str(message.model_dump())
            for message in plan.messages_to_summarize
        )
        loaded.append_compaction(
            summary="calculated",
            first_kept_entry_id=plan.first_kept_entry_id,
            tokens_before=plan.tokens_before.tokens,
            system_message=plan.system_message,
        )
        reloaded = JsonlSession.load(session.path)
        assert (
            any(isinstance(item, ToolCatalogEntry) for item in reloaded.entries)
            and reloaded.replay().messages
        )
        pid = session._registry._mcp.clients["local"].pid
    finally:
        session.close()
    assert_dead(pid)
    prefix = (tmp_path / "calls.txt").read_text()
    llm = FakeLLMClient(
        [
            assistant(
                tool_calls=[tool_call("call2", tool_alias("local", "calc.add"), {"value": 4})]
            ),
            assistant("unavailable"),
        ]
    )
    restored = AgentSession.resume(
        session.path,
        cwd=tmp_path,
        registry=build_default_registry(Workspace(tmp_path)),
        llm_factory=lambda *args: llm,
    )
    try:
        restored.run("calculate again")
        result = next(
            item
            for item in restored.state.messages
            if isinstance(item, ToolMessage) and item.tool_call_id == "call2"
        )
        assert result.is_error and "unknown tool" in result.content
        assert (tmp_path / "calls.txt").read_text() == prefix
        patch = next(
            item
            for item in restored.state.messages
            if isinstance(item, SystemMessage) and item.section_patch
        )
        assert "mcp_local" not in next(
            change.content for change in patch.section_patch if change.id == "tools"
        )
        assert len(restored._registry.schemas()) == 8
    finally:
        restored.close()


def test_catalog_write_failure_installs_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """目录 fsync 失败时没有 tools/call，进程清理、user 未提交、工具未安装。"""
    session = runtime(tmp_path)

    def fail(*args: object) -> None:
        """仅目录审计无法落盘，启动允许审计仍写入。"""
        raise SessionError("catalog disk failed")

    monkeypatch.setattr(session._session, "append_tool_catalog", fail)
    result = session.run("calculate")
    assert result.stop_reason == "error" and len(session._registry.schemas()) == 8
    assert not session.state.messages and not (tmp_path / "calls.txt").exists()
    assert session._registry._mcp.clients == {}


def test_schema_reload_patch_precedes_next_user_and_updates_prediction(tmp_path: Path) -> None:
    """刷新冻结目录后，schema/描述 patch 与预测按新工具提交，旧参数明确拒绝。"""
    session = runtime(tmp_path)
    try:
        session.run("first")
        before = session.request_snapshot()
        (tmp_path / "catalog.json").write_text(
            json.dumps(
                [
                    {
                        "name": "calc.add",
                        "description": "new schema",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"new_value": {"type": "string"}},
                            "required": ["new_value"],
                        },
                    }
                ]
            )
        )
        updated = config(tmp_path)
        updated = UserConfig(
            servers={
                "local": updated.servers["local"].model_copy(
                    update={"tools": ("calc.add",), "readonly_tools": ("calc.add",)}
                )
            }
        )
        session.reload_mcp(updated)
        assert len(session.tool_schemas) == 9
        session._llm._script = [
            assistant(
                tool_calls=[tool_call("newcall", tool_alias("local", "calc.add"), {"value": 1})]
            ),
            assistant("invalid arguments"),
        ]
        session.run("second")
        assert session.request_snapshot().tools != before.tools
        messages = JsonlSession.load(session.path).replay().messages
        user_at = next(
            index
            for index, item in enumerate(messages)
            if item.role == "user" and item.content == "second"
        )
        assert (
            isinstance(messages[user_at - 1], SystemMessage) and messages[user_at - 1].section_patch
        )
        assert messages[-2].is_error and (tmp_path / "calls.txt").read_text().splitlines() == [
            "calc.add"
        ]
        catalogs = [
            entry
            for entry in JsonlSession.load(session.path).entries
            if isinstance(entry, ToolCatalogEntry)
        ]
        assert len(catalogs) == 2 and catalogs[0].fingerprint != catalogs[1].fingerprint
    finally:
        session.close()


def test_historical_alias_rebinding_fails_before_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """新配置出现旧别名但指向不同工具时，durable 目录与 Registry 都拒绝。"""
    session = runtime(tmp_path)
    try:
        session.run("first")
        alias = tool_alias("local", "calc.add")
        monkeypatch.setattr("mini_pi.mcp.catalog.tool_alias", lambda *args: alias)
        changed = config(tmp_path)
        changed = UserConfig(
            servers={
                "local": changed.servers["local"].model_copy(
                    update={"tools": ("other",), "readonly_tools": ("other",)}
                )
            }
        )
        with pytest.raises(SessionError, match="identity"):
            session.reload_mcp(changed)
        assert len(session.tool_schemas) == 8 and session._registry._mcp.clients == {}
    finally:
        session.close()


def test_new_and_real_sigint_cleanup_connections(tmp_path: Path) -> None:
    """new 只继承配置不继承进程，真正 SIGINT 取消调用后保持消息配对并清理。"""
    session = runtime(tmp_path, "hang")
    session._registry.prepare_mcp()
    pid = session._registry._mcp.clients["local"].pid
    new = session.new(sessions_root=tmp_path / "new")
    assert_dead(pid)
    new._registry.prepare_mcp()
    new_pid = new._registry._mcp.clients["local"].pid
    previous = signal.signal(signal.SIGALRM, lambda *args: os.kill(os.getpid(), signal.SIGINT))
    signal.setitimer(signal.ITIMER_REAL, 0.15)
    try:
        result = new.run("calculate")
        assert result.stop_reason == "cancelled"
        messages = JsonlSession.load(new.path).replay().messages
        assert messages[-1].is_error and messages[-1].tool_call_id == messages[-2].tool_calls[0].id
        assert (tmp_path / "calls.txt").read_text().splitlines() == ["calc.add"]
        assert_dead(new_pid)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        new.close()


def test_cli_tools_reload_and_exit_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """真实 CLI 装配显示来源，reload 是本地命令，正常退出不遗留进程。"""
    llm = FakeLLMClient([assistant("done")])
    monkeypatch.setattr("mini_pi.cli.app.load_config", lambda: config(tmp_path))
    monkeypatch.setattr("mini_pi.cli.app.create_llm", lambda *args, **kwargs: llm)
    monkeypatch.setattr(
        "mini_pi.cli.app.create_approval_handler",
        lambda renderer: lambda request: ApprovalResponse("allow"),
    )
    monkeypatch.setattr("mini_pi.cli.app.load_last_connection", lambda: None)
    monkeypatch.setattr(
        "mini_pi.cli.app.save_last_connection", lambda *args: tmp_path / "auth.json"
    )
    monkeypatch.setattr("mini_pi.session.jsonl._sessions_root", lambda path: tmp_path / "sessions")
    started: list[int] = []
    original_start = SecurityPolicy.start_server

    def start(self: SecurityPolicy, server_id: str) -> subprocess.Popen[bytes]:
        """记录宿主启动的真实身份，退出后逐一查进程。"""
        process = original_start(self, server_id)
        started.append(process.pid)
        return process

    monkeypatch.setattr(SecurityPolicy, "start_server", start)
    result = CliRunner().invoke(
        app,
        ["--cwd", str(tmp_path), "--no-banner"],
        input="task\n/tools\n/mcp reload\n/tools\n/exit\n",
    )
    assert result.exit_code == 0, result.output
    assert "MCP ready: local" in result.output and "External MCP [local/calc.add]" in result.output
    assert len(llm.calls) == 1 and len(started) == 2
    for pid in started:
        assert_dead(pid)
