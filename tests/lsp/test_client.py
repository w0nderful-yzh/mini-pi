"""真实离线子进程覆盖 LSP 查询、位置、诊断版本和协议生命周期。"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

from mini_pi.config import ServerConfig, UserConfig
from mini_pi.interaction import ApprovalRequest, ApprovalResponse
from mini_pi.lsp.client import MAX_RESULT_BYTES, LspClient
from mini_pi.lsp.transport import LspError
from mini_pi.security import SecurityPolicy
from mini_pi.tools import build_default_registry
from mini_pi.workspace.workspace import Workspace

SERVER = Path(__file__).parents[1] / "fixtures" / "lsp" / "server.py"


def make_client(
    tmp_path: Path, mode: str = "normal", *, decisions: list[ApprovalRequest] | None = None
) -> LspClient:
    """以严格配置装配真实 fake server，批准仅由测试宿主显式提供。"""

    def approve(request: ApprovalRequest) -> ApprovalResponse:
        """记录实际启动审批，不把审批转为模型工具结果。"""
        if decisions is not None:
            decisions.append(request)
        return ApprovalResponse("allow")

    config = UserConfig(
        servers={
            "python": ServerConfig(
                kind="lsp",
                argv=(sys.executable, str(SERVER), mode, str(tmp_path / "events.jsonl")),
                timeout=0.5,
            )
        }
    )
    return LspClient(
        Workspace(tmp_path), SecurityPolicy(cwd=tmp_path, config=config, approval=approve), "python"
    )


def read_events(tmp_path: Path) -> list[dict]:
    """只读取 fake server 实际收到的帧，不依赖预设调用次数。"""
    return [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]


def assert_dead(pid: int) -> None:
    """确认进程已回收，不能把关闭标记当作资源清理证据。"""
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_diagnostic_output_has_byte_cap_and_truncation(tmp_path: Path) -> None:
    """多字节长诊断保留合法 JSON，条目和消息截断都明确标记。"""
    (tmp_path / "a.py").write_text("a = 1\n")
    client = make_client(tmp_path, "large_diagnostics")
    try:
        content = client.diagnostics("a.py")
        result = json.loads(content)
        assert len(content.encode("utf-8")) <= MAX_RESULT_BYTES
        assert result["truncated"] and 0 < len(result["diagnostics"]) < 100
        assert all(len(item["message"]) == 4000 for item in result["diagnostics"])
    finally:
        client.close()


def test_server_environment_secret_is_redacted_before_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """带引号和 Unicode 的配置凭据不能经 server 字符串进入模型结果。"""
    secret = 'secret"中文😀'
    monkeypatch.setenv("LSP_TEST_SECRET", secret)
    (tmp_path / "a.py").write_text("a = 1\n")
    config = UserConfig(
        servers={
            "python": ServerConfig(
                kind="lsp",
                argv=(sys.executable, str(SERVER), "secret", str(tmp_path / "events.jsonl")),
                env={"TEST_TOKEN": "LSP_TEST_SECRET"},
                timeout=0.5,
            )
        }
    )
    client = LspClient(
        Workspace(tmp_path),
        SecurityPolicy(
            cwd=tmp_path, config=config, approval=lambda request: ApprovalResponse("allow")
        ),
        "python",
    )
    try:
        result = json.loads(client.diagnostics("a.py"))
        assert result["diagnostics"][0]["message"] == "[REDACTED]"
    finally:
        client.close()


def test_configured_tools_are_lazy_strict_and_document_bounded(tmp_path: Path) -> None:
    """显式启用增加三个工具，但注册、非法参数和超大文件均不启动进程。"""
    from mini_pi.errors import ToolError

    client = make_client(tmp_path)
    registry = build_default_registry(Workspace(tmp_path), security=client._security)
    assert len(registry.schemas()) == 11 and registry._lsp.pid is None
    (tmp_path / "a.py").write_text("a = 1\n")
    for arguments in [
        {"path": "a.py", "line": True, "column": 1},
        {"path": "a.py", "line": 1, "column": 1, "server": "other"},
        {"path": "../outside.py", "line": 1, "column": 1},
    ]:
        with pytest.raises(ToolError):
            registry.execute("lsp_definition", arguments)
    (tmp_path / "a.py").write_bytes(b"x" * 1_000_001)
    with pytest.raises(ToolError, match="byte limit"):
        registry.execute("lsp_diagnostics", {"path": "a.py"})
    assert registry._lsp.pid is None and not (tmp_path / "events.jsonl").exists()


def test_config_rejects_ambiguous_servers_and_invalid_timeout() -> None:
    """多 server 和非法等待上限明确拒绝，不猜测默认语言选择。"""
    from pydantic import ValidationError

    server = ServerConfig(kind="lsp", argv=("pyright-langserver", "--stdio"))
    with pytest.raises(ValidationError):
        UserConfig(servers={"one": server, "two": server})
    for timeout in (0, 121, float("nan"), float("inf")):
        with pytest.raises(ValidationError):
            ServerConfig(kind="lsp", argv=("server",), timeout=timeout)


def test_escaped_document_exceeding_frame_limit_discards_connection(tmp_path: Path) -> None:
    """文件未超限但 JSON 转义后帧过大时，不保留未成功 didOpen 的宿主状态。"""
    path = tmp_path / "a.py"
    path.write_text("#" + '"' * 999_999)
    approvals: list[ApprovalRequest] = []
    client = make_client(tmp_path, decisions=approvals)
    try:
        with pytest.raises(LspError, match="outgoing frame"):
            client.diagnostics("a.py")
        assert client.pid is None
        path.write_text("a = 1\n")
        assert json.loads(client.diagnostics("a.py"))["version"] == 1
        assert len(approvals) == 2
    finally:
        client.close()


@pytest.mark.parametrize("mode,offset", [("normal", 4), ("utf8", 10), ("utf32", 3), ("link", 4)])
def test_unicode_positions_and_location_variants(mode: str, offset: int, tmp_path: Path) -> None:
    """中文和 emoji 使用协商编码，Location/LocationLink 都还原字符列。"""
    (tmp_path / "a.py").write_text("你好😀目标 = 1\n")
    approvals: list[ApprovalRequest] = []
    client = make_client(tmp_path, mode, decisions=approvals)
    try:
        result = json.loads(client.locations("textDocument/definition", "a.py", 1, 4))
        assert result["locations"][0]["range"]["start"] == {"line": 1, "column": 4}
        pid = client.pid
        refs = json.loads(client.locations("textDocument/references", "a.py", 1, 4))
        assert len(refs["locations"]) == 2 and client.pid == pid
        assert len(approvals) == 1
    finally:
        client.close()
    assert pid is not None
    assert_dead(pid)
    events = read_events(tmp_path)
    query = next(item for item in events if item.get("method") == "textDocument/definition")
    assert query["params"]["position"] == {"line": 0, "character": offset}
    assert [
        item["method"]
        for item in events
        if item.get("method") in {"shutdown", "exit", "textDocument/didClose"}
    ] == ["textDocument/didClose", "shutdown", "exit"]


@pytest.mark.parametrize("mode", ["normal", "full_sync", "late_diagnostics"])
def test_diagnostics_follow_disk_changes_and_clear_old_errors(mode: str, tmp_path: Path) -> None:
    """外部直接改文件也同步；当前无错误有版本证据，不复用旧诊断。"""
    path = tmp_path / "a.py"
    path.write_text("bad = 1\n")
    client = make_client(tmp_path, mode)
    try:
        first = json.loads(client.diagnostics("a.py"))
        assert first["version"] == 1 and len(first["diagnostics"]) == 1
        path.write_text("good = 1\n")
        second = json.loads(client.diagnostics("a.py"))
        assert second["version"] == 2 and second["diagnostics"] == [] and second["fresh"]
        assert first["content_hash"] != second["content_hash"]
        path.write_text("bad_again = 1\n")
        third = json.loads(client.diagnostics("a.py"))
        assert third["version"] == 3 and len(third["diagnostics"]) == 1
    finally:
        client.close()
    changes = [
        item for item in read_events(tmp_path) if item.get("method") == "textDocument/didChange"
    ]
    assert ("range" in changes[0]["params"]["contentChanges"][0]) == (mode != "full_sync")


def test_open_dependency_changes_invalidate_query_file_version(tmp_path: Path) -> None:
    """已打开依赖文件的外部改动同步后，提高当前文件版本再等新诊断。"""
    (tmp_path / "a.py").write_text("good = 1\n")
    (tmp_path / "b.py").write_text("good = 2\n")
    client = make_client(tmp_path, "dependency")
    try:
        assert json.loads(client.diagnostics("a.py"))["diagnostics"] == []
        client.diagnostics("b.py")
        (tmp_path / "b.py").write_text("bad = 3\n")
        current = json.loads(client.diagnostics("a.py"))
        assert current["version"] == 2 and len(current["diagnostics"]) == 1
    finally:
        client.close()


@pytest.mark.parametrize("mode", ["unversioned", "stale", "silent", "unchanged_pull"])
def test_unready_or_ambiguous_diagnostics_are_errors(mode: str, tmp_path: Path) -> None:
    """缺版本、旧版本、未就绪或无 previousResultId 的 unchanged 都不能冒充零诊断。"""
    (tmp_path / "a.py").write_text("good = 1\n")
    client = make_client(tmp_path, mode)
    try:
        with pytest.raises(LspError):
            client.diagnostics("a.py")
    finally:
        client.close()


def test_pull_diagnostics_are_requested_after_synchronization(tmp_path: Path) -> None:
    """声明 pull 能力时使用当前版本请求，不依赖旧的推送缓存。"""
    (tmp_path / "a.py").write_text("good = 1\n")
    client = make_client(tmp_path, "pull")
    try:
        assert json.loads(client.diagnostics("a.py"))["fresh"]
        (tmp_path / "a.py").write_text("good = 2\n")
        assert json.loads(client.diagnostics("a.py"))["version"] == 2
    finally:
        client.close()
    methods = [item.get("method") for item in read_events(tmp_path)]
    assert methods.index("textDocument/didChange") < methods.index(
        "textDocument/diagnostic", methods.index("textDocument/didChange")
    )


@pytest.mark.parametrize(
    "mode",
    [
        "hang",
        "crash",
        "bad_frame",
        "wrong_id",
        "init_error",
        "no_sync",
        "malformed_uri",
        "bad_encoding",
    ],
)
def test_protocol_failure_cleans_process_and_requires_new_approval(
    mode: str, tmp_path: Path
) -> None:
    """失败后没有自动重放；下一次显式调用重新审批，原进程已被清理。"""
    (tmp_path / "a.py").write_text("a = 1\n")
    approvals: list[ApprovalRequest] = []
    client = make_client(tmp_path, mode, decisions=approvals)
    try:
        for _ in range(2):
            with pytest.raises(LspError):
                client.locations("textDocument/definition", "a.py", 1, 1)
            assert client.pid is None
        assert len(approvals) == 2
    finally:
        client.close()


def test_server_control_requests_cannot_edit_or_expand_roots(tmp_path: Path) -> None:
    """交错反向请求收到配置、根目录或明确协议错误，不执行 applyEdit。"""
    (tmp_path / "a.py").write_text("a = 1\n")
    client = make_client(tmp_path, "controls")
    try:
        client.locations("textDocument/definition", "a.py", 1, 1)
    finally:
        client.close()
    responses = {item["id"]: item for item in read_events(tmp_path) if "method" not in item}
    assert responses["config"]["result"][0]["analysis"]["diagnosticMode"] == "openFilesOnly"
    assert responses["folders"]["result"][0]["uri"] == tmp_path.resolve().as_uri()
    assert responses["edit"]["error"]["code"] == responses["unknown"]["error"]["code"] == -32601
    assert (tmp_path / "a.py").read_text() == "a = 1\n"


@pytest.mark.parametrize("mode", ["outside", "many", "no_definitions", "changed_during_query"])
def test_restriction_truncation_capability_and_race(mode: str, tmp_path: Path) -> None:
    """外部路径不读、过多结果标记截断、不支持或等待中改动都明确报告。"""
    (tmp_path / "a.py").write_text("a = 1\n")
    client = make_client(tmp_path, mode)
    try:
        if mode in {"no_definitions", "changed_during_query"}:
            with pytest.raises(LspError):
                client.locations("textDocument/definition", "a.py", 1, 1)
        else:
            result = json.loads(client.locations("textDocument/references", "a.py", 1, 1))
            if mode == "outside":
                assert all(
                    item["restricted"] and "path" not in item for item in result["locations"]
                )
            else:
                assert result["truncated"] and len(result["locations"]) == 100
    finally:
        client.close()


def test_invalid_position_is_rejected_before_process_start(tmp_path: Path) -> None:
    """非法位置不应该提示审批或启动 server。"""
    (tmp_path / "a.py").write_text("a = 1\n")
    client = make_client(tmp_path)
    with pytest.raises(LspError):
        client.locations("textDocument/definition", "a.py", 2, 9)
    assert client.pid is None and not (tmp_path / "events.jsonl").exists()


def test_shutdown_timeout_still_reaps_process(tmp_path: Path) -> None:
    """server 不响应 shutdown 时在有限时间内清理，不能等待到自然退出。"""
    (tmp_path / "a.py").write_text("a = 1\n")
    client = make_client(tmp_path, "hang_shutdown")
    client.locations("textDocument/definition", "a.py", 1, 1)
    pid = client.pid
    started = time.monotonic()
    client.close()
    assert time.monotonic() - started < 2
    assert pid is not None
    assert_dead(pid)


def test_default_tools_are_disabled_without_explicit_server(tmp_path: Path) -> None:
    """不读取仓库 server 文件，不发现即启动，也不暴露未配置的 LSP schema。"""
    (tmp_path / "AGENTS.md").write_text("enable lsp server automatically")
    registry = build_default_registry(Workspace(tmp_path))
    assert not any(schema.name.startswith("lsp_") for schema in registry.schemas())
