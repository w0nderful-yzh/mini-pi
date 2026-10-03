"""Session 和 CLI 的真实 LSP 生命周期及文件工具同步边界。"""

from __future__ import annotations

import json
import os
import signal
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mini_pi.cli.app import app
from mini_pi.config import ServerConfig, UserConfig
from mini_pi.errors import SessionError
from mini_pi.interaction import ApprovalResponse
from mini_pi.lsp.transport import StdioTransport
from mini_pi.security import SecurityPolicy
from mini_pi.session.jsonl import JsonlSession
from mini_pi.session.models import ControlEntry
from mini_pi.session.runtime import AgentSession
from mini_pi.tools import build_default_registry
from mini_pi.workspace.workspace import Workspace
from tests.conftest import FakeLLMClient, assistant, tool_call
from tests.lsp.test_client import SERVER, assert_dead, read_events


def configuration(tmp_path: Path, mode: str = "normal") -> UserConfig:
    """显式配置唯一 Python server，离线验收不依赖用户安装或认证。"""
    import sys

    return UserConfig(
        servers={
            "python": ServerConfig(
                kind="lsp",
                argv=(sys.executable, str(SERVER), mode, str(tmp_path / "events.jsonl")),
                timeout=0.5,
            )
        }
    )


def runtime(tmp_path: Path, *, mode: str = "normal", decision: str = "allow") -> AgentSession:
    """装配 durable Session，启动审计必须先于真实进程查询。"""
    return AgentSession.create(
        cwd=tmp_path,
        provider="deepseek",
        model="deepseek-flash",
        sessions_root=tmp_path / "sessions",
        llm=FakeLLMClient(
            [
                assistant(
                    tool_calls=[
                        tool_call("d1", "lsp_definition", {"path": "a.py", "line": 1, "column": 1})
                    ]
                ),
                assistant("done"),
            ]
        ),
        registry=build_default_registry(
            Workspace(tmp_path),
            security=SecurityPolicy(
                cwd=tmp_path,
                config=configuration(tmp_path, mode),
                approval=lambda request: ApprovalResponse(decision),
            ),
        ),
    )


@pytest.mark.parametrize("method", ["edit", "write", "bash", "external"])
def test_all_file_change_sources_invalidate_diagnostics(method: str, tmp_path: Path) -> None:
    """同步读真实磁盘，edit/write/bash 和未记录的外部修改均提高诊断版本。"""
    path = tmp_path / "a.py"
    path.write_text("bad = 1\n")
    session = runtime(tmp_path)
    registry = session._registry
    try:
        before = json.loads(registry.execute("lsp_diagnostics", {"path": "a.py"}).content)
        if method == "edit":
            registry.execute(
                "edit", {"path": "a.py", "edits": [{"old_text": "bad", "new_text": "good"}]}
            )
        elif method == "write":
            registry.execute("write", {"path": "a.py", "content": "good = 1\n"})
        elif method == "bash":
            registry.execute("bash", {"command": "printf 'good = 1\\n' > a.py"})
        else:
            path.write_text("good = 1\n")
        after = json.loads(registry.execute("lsp_diagnostics", {"path": "a.py"}).content)
        assert before["diagnostics"] and after["diagnostics"] == []
        assert after["version"] == 2 and after["content_hash"] != before["content_hash"]
    finally:
        session.close()


def test_new_and_resume_own_fresh_connections_and_audit(tmp_path: Path) -> None:
    """new 清理旧连接，恢复不继承启动授权，审计仅提交至所属 Session。"""
    (tmp_path / "a.py").write_text("a = 1\n")
    old = runtime(tmp_path)
    old.run("definition")
    old_pid = old._registry._lsp.pid
    prefix = old.path.read_bytes()
    new = old.new(sessions_root=tmp_path / "new")
    assert_dead(old_pid)
    assert new._registry._lsp is not old._registry._lsp
    try:
        new._registry.execute("lsp_definition", {"path": "a.py", "line": 1, "column": 1})
        assert old.path.read_bytes() == prefix
        assert (
            sum(isinstance(entry, ControlEntry) for entry in JsonlSession.load(new.path).entries)
            == 1
        )
    finally:
        new.close()
    denied = AgentSession.resume(
        old.path,
        cwd=tmp_path,
        llm_factory=lambda *args: FakeLLMClient([]),
        registry=runtime(tmp_path, decision="deny")._registry,
    )
    try:
        from mini_pi.errors import ToolError

        with pytest.raises(ToolError):
            denied._registry.execute("lsp_definition", {"path": "a.py", "line": 1, "column": 1})
        assert denied._registry._lsp.pid is None
        assert [
            entry.decision
            for entry in JsonlSession.load(old.path).entries
            if isinstance(entry, ControlEntry)
        ] == ["allow", "deny"]
    finally:
        denied.close()


def test_audit_failure_prevents_server_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """启动授权不能落盘时，不发 initialize 且不创建 server。"""
    (tmp_path / "a.py").write_text("a = 1\n")
    session = runtime(tmp_path)

    def fail(*args: object) -> None:
        """模拟 fsync 前的 Session 写入失败。"""
        raise SessionError("disk failed")

    monkeypatch.setattr(session._session, "append_control", fail)
    assert session.run("query").stop_reason == "error"
    assert not (tmp_path / "events.jsonl").exists() and session._registry._lsp.pid is None


def test_real_sigint_cancels_query_and_preserves_tool_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真实 SIGINT 打断等待后取消请求、回收进程，并保留完整工具观察。"""
    (tmp_path / "a.py").write_text("a = 1\n")
    session = runtime(tmp_path, mode="hang")
    sent: list[dict] = []
    pids: list[int] = []
    original_send = StdioTransport.send

    def send(self: StdioTransport, message: dict, *, timeout: float) -> None:
        """记录已写入真实管道的帧，挂起 server 未必来得及读取取消通知。"""
        original_send(self, message, timeout=timeout)
        sent.append(message)
        pids.append(self.process.pid)

    monkeypatch.setattr(StdioTransport, "send", send)
    previous = signal.signal(signal.SIGALRM, lambda *args: os.kill(os.getpid(), signal.SIGINT))
    signal.setitimer(signal.ITIMER_REAL, 0.3)
    try:
        result = session.run("query")
        assert result.stop_reason == "cancelled" and session._registry._lsp.pid is None
        messages = JsonlSession.load(session.path).replay().messages
        assert messages[-1].role == "tool" and messages[-1].is_error
        assert messages[-2].tool_calls[0].id == messages[-1].tool_call_id
        assert any(item.get("method") == "$/cancelRequest" for item in sent)
        assert any(
            item.get("method") == "textDocument/definition" for item in read_events(tmp_path)
        )
        assert_dead(pids[-1])
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        session.close()


def test_interrupt_before_loop_also_closes_existing_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """压缩预检中的中断不能留下上一任务的 LSP 进程。"""
    (tmp_path / "a.py").write_text("a = 1\n")
    session = runtime(tmp_path)
    session.run("query")
    pid = session._registry._lsp.pid

    def interrupt(**kwargs: object) -> None:
        """模拟进入 Agent 之前的模型摘要被取消。"""
        raise KeyboardInterrupt

    monkeypatch.setattr(session, "_auto_compact_if_needed", interrupt)
    with pytest.raises(KeyboardInterrupt):
        session.run("next query")
    assert_dead(pid)


@pytest.mark.parametrize("one_shot", [True, False])
def test_cli_exit_and_new_close_actual_processes(
    one_shot: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Typer 正常退出和 REPL new 使用实际进程验证清理，无须宿主手动 close。"""
    (tmp_path / "a.py").write_text("a = 1\n")
    script = [
        assistant(
            tool_calls=[tool_call("a", "lsp_definition", {"path": "a.py", "line": 1, "column": 1})]
        ),
        assistant("done"),
    ]
    llm = FakeLLMClient(script if one_shot else script * 2)
    monkeypatch.setattr("mini_pi.cli.app.load_config", lambda: configuration(tmp_path))
    monkeypatch.setattr(
        "mini_pi.cli.app.create_approval_handler",
        lambda renderer: lambda request: ApprovalResponse("allow"),
    )
    monkeypatch.setattr("mini_pi.cli.app.create_llm", lambda *args, **kwargs: llm)
    monkeypatch.setattr("mini_pi.cli.app.load_last_connection", lambda: None)
    monkeypatch.setattr(
        "mini_pi.cli.app.save_last_connection", lambda *args: tmp_path / "auth.json"
    )
    monkeypatch.setattr("mini_pi.session.jsonl._sessions_root", lambda path: tmp_path / "sessions")
    pids: list[int] = []
    start_server = SecurityPolicy.start_server

    def start(self: SecurityPolicy, server_id: str) -> subprocess.Popen[bytes]:
        """记录真实 Popen 身份，第二次启动时先验证旧 Session 已回收。"""
        if pids:
            assert_dead(pids[-1])
        process = start_server(self, server_id)
        pids.append(process.pid)
        return process

    monkeypatch.setattr(SecurityPolicy, "start_server", start)
    args = ["--cwd", str(tmp_path), "--no-banner"] + (["query"] if one_shot else [])
    result = CliRunner().invoke(
        app, args, input=None if one_shot else "query\n/new\nquery\n/exit\n"
    )
    assert result.exit_code == 0, result.output
    assert len(pids) == (1 if one_shot else 2)
    for pid in pids:
        assert_dead(pid)
