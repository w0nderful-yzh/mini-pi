"""官方 SDK 与真实离线子进程的 MCP 协议往返和清理验收。"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from mini_pi.config import ServerConfig, UserConfig
from mini_pi.errors import ToolError
from mini_pi.interaction import ApprovalRequest, ApprovalResponse
from mini_pi.mcp.client import MCPClient, MCPError
from mini_pi.security import SecurityPolicy
from tests.lsp.test_client import assert_dead

SERVER = Path(__file__).parents[1] / "fixtures" / "mcp" / "server.py"


def test_background_defect_bubbles_after_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """后台程序缺陷回到宿主线程，先回收进程而非吞成普通工具错误。"""
    connected = client(tmp_path)
    try:
        connected.list_tools()
        pid = connected.pid

        async def broken(line: bytes, output: Any) -> None:
            """在连接建立后的响应路径注入真正的程序缺陷。"""
            raise RuntimeError("injected protocol implementation defect")

        monkeypatch.setattr(connected, "_incoming", broken)
        with pytest.raises(ExceptionGroup) as failure:
            connected.call_tool("calc.add", {"value": 1})
        assert failure.value.subgroup(RuntimeError) is not None
        assert_dead(pid)
    finally:
        connected.close()


def config(tmp_path: Path, mode: str = "normal", *, readonly: bool = True) -> UserConfig:
    """只配置固定测试子进程和明确白名单，未涉及模型或全局用户文件。"""
    return UserConfig(
        servers={
            "local": ServerConfig(
                kind="mcp",
                argv=(sys.executable, str(SERVER), mode, str(tmp_path / "events.jsonl")),
                tools=("calc.add", "mutate"),
                readonly_tools=("calc.add",) if readonly else (),
                timeout=0.5,
            )
        }
    )


def client(
    tmp_path: Path, mode: str = "normal", *, decisions: list[ApprovalRequest] | None = None
) -> MCPClient:
    """注入显式允许的宿主回调，并记录实际审批次数。"""

    def approve(request: ApprovalRequest) -> ApprovalResponse:
        """记录一次具体请求，没有历史批准缓存。"""
        if decisions is not None:
            decisions.append(request)
        return ApprovalResponse("allow")

    return MCPClient(
        SecurityPolicy(cwd=tmp_path, config=config(tmp_path, mode), approval=approve), "local"
    )


def events(tmp_path: Path) -> list[dict]:
    """只读取测试 server 实际收到的帧。"""
    return [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]


def test_official_sdk_paging_reuse_and_close(tmp_path: Path) -> None:
    """初始化 capabilities 为空、分页正确、线程和进程复用且全部回收。"""
    approvals: list[ApprovalRequest] = []
    connected = client(tmp_path, decisions=approvals)
    try:
        assert [tool.name for tool in connected.list_tools()] == ["calc.add", "mutate", "other"]
        pid, thread = connected.pid, connected._thread
        assert connected.call_tool("calc.add", {"value": 2}).structuredContent == {"answer": 3}
        assert connected.call_tool("calc.add", {"value": 5}).structuredContent == {"answer": 6}
        assert connected.pid == pid and connected._thread is thread and len(approvals) == 1
    finally:
        connected.close()
    assert_dead(pid)
    assert not thread.is_alive()
    messages = events(tmp_path)
    assert messages[0]["method"] == "initialize" and messages[0]["params"]["capabilities"] == {}
    assert messages[1]["method"] == "notifications/initialized"
    assert len([item for item in messages if item.get("method") == "tools/list"]) == 2


@pytest.mark.parametrize(
    "mode",
    ["version", "capability", "wrong_id", "log", "oversized", "cursor", "crash", "init_hang"],
)
def test_protocol_failures_cleanup_without_replay(mode: str, tmp_path: Path) -> None:
    """版本、能力、id、日志污染、帧大小、分页、断连与超时都关闭连接。"""
    connected = client(tmp_path, mode)
    try:
        with pytest.raises(MCPError):
            connected.list_tools()
        assert connected.pid is None and connected._thread is None
    finally:
        connected.close()


@pytest.mark.parametrize("mode", ["hang", "call_crash"])
def test_call_failure_never_replays_side_effect(mode: str, tmp_path: Path) -> None:
    """已写副作用的失败调用只出现一次，失效连接上的新调用明确拒绝。"""
    connected = client(tmp_path, mode)
    connected.list_tools()
    pid = connected.pid
    with pytest.raises(MCPError):
        connected.call_tool("mutate", {"value": 1})
    with pytest.raises(MCPError, match="reload"):
        connected.call_tool("mutate", {"value": 1})
    assert (tmp_path / "calls.txt").read_text().splitlines() == ["mutate"]
    assert_dead(pid)


def test_server_requests_are_rejected_without_advertising_capabilities(tmp_path: Path) -> None:
    """sampling/elicitation/roots/未知请求均明确拒绝，宿主没有模型或 stdin 操作。"""
    connected = client(tmp_path, "requests")
    try:
        connected.list_tools()
    finally:
        connected.close()
    responses = [item for item in events(tmp_path) if "error" in item]
    assert len(responses) == 8 and all(item["error"]["code"] == -32601 for item in responses)


def test_denied_start_creates_no_process(tmp_path: Path) -> None:
    """启动拒绝是审批事实，不产生 initialize 或本地进程。"""
    connected = MCPClient(
        SecurityPolicy(
            cwd=tmp_path, config=config(tmp_path), approval=lambda request: ApprovalResponse("deny")
        ),
        "local",
    )
    with pytest.raises(ToolError):
        connected.list_tools()
    assert connected.pid is None and not (tmp_path / "events.jsonl").exists()


def test_stderr_and_secret_environment_are_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """大量 stderr 不堵塞；只转发明确环境引用，错误尾部不泄露秘密。"""
    monkeypatch.setenv("MCP_TEST_SECRET", 'secret"中文')
    configured = config(tmp_path, "stderr")
    server = configured.servers["local"].model_copy(
        update={"env": {"TEST_SECRET": "MCP_TEST_SECRET"}}
    )
    connected = MCPClient(
        SecurityPolicy(
            cwd=tmp_path,
            config=UserConfig(servers={"local": server}),
            approval=lambda request: ApprovalResponse("allow"),
        ),
        "local",
    )
    try:
        connected.list_tools()
        assert len(connected._stderr) <= 16_384
        assert "secret" not in str(connected._error("closed"))
    finally:
        connected.close()


def test_hanging_exit_is_bounded(tmp_path: Path) -> None:
    """server 不响应 EOF 也在有限时间内结束整个组和事件循环。"""
    connected = client(tmp_path, "hang_exit")
    connected.list_tools()
    pid = connected.pid
    start = time.monotonic()
    connected.close()
    assert time.monotonic() - start < 4
    assert_dead(pid)


def test_child_process_group_is_cleaned(tmp_path: Path) -> None:
    """直接进程正常退出也不能留下继承管道的孙进程。"""
    connected = client(tmp_path, "descendant")
    try:
        connected.list_tools()
        child_pid = int((tmp_path / "child.pid").read_text())
        pid = connected.pid
    finally:
        connected.close()
    assert_dead(pid)
    status = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(child_pid)], capture_output=True, text=True
    ).stdout.strip()
    assert not status or status.startswith("Z")


def test_provider_and_unrelated_secrets_do_not_enter_server_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MCP 真实进程仅得到宿主明确最小环境，不继承 Provider 或任意秘密。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-provider-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "fake-provider-secret")
    monkeypatch.setenv("UNRELATED_SECRET", "unrelated-secret")
    connected = client(tmp_path, "environment")
    try:
        connected.list_tools()
        inherited = json.loads(connected.call_tool("calc.add", {"value": 1}).content[0].text)
        assert not {"DEEPSEEK_API_KEY", "OPENAI_API_KEY", "UNRELATED_SECRET"} & set(inherited)
    finally:
        connected.close()
