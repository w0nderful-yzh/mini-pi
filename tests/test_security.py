"""权限、一次性绑定、用户配置和真实子进程环境的离线边界测试。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from mini_pi.config import ServerConfig, UserConfig, load_config
from mini_pi.errors import (
    InteractionUnavailableError,
    MiniPiError,
    PermissionDeniedError,
    ToolArgumentError,
)
from mini_pi.interaction import ApprovalRequest, ApprovalResponse
from mini_pi.security import ControlDecision, SecurityPolicy
from mini_pi.tools import build_default_registry
from mini_pi.tools.bash import BashTool
from mini_pi.tools.process import run_shell
from mini_pi.tools.registry import ToolRegistry
from mini_pi.workspace.workspace import Workspace


@pytest.mark.parametrize(
    "mode,decision", [("workspace", "allow"), ("workspace", "deny"), ("readonly", "allow")]
)
def test_direct_registry_checks_before_real_shell(mode: str, decision: str, tmp_path: Path) -> None:
    """直接调用 Registry 也无法绕过权限，只有本次允许才创建标记文件。"""
    requests: list[ApprovalRequest] = []
    records: list[ControlDecision] = []

    def approve(request: ApprovalRequest) -> ApprovalResponse:
        """批准前真实文件尚不存在，记录本次完整参数。"""
        assert not (tmp_path / "marker").exists()
        requests.append(request)
        return ApprovalResponse(decision)

    registry = ToolRegistry(
        SecurityPolicy(
            cwd=tmp_path,
            config=UserConfig(permissions=mode),
            approval=approve,
            record=records.append,
        )
    )
    registry.register(BashTool(Workspace(tmp_path)))
    args = {"command": "printf approved > marker"}
    if mode == "readonly" or decision == "deny":
        with pytest.raises(PermissionDeniedError):
            registry.execute("bash", args)
        assert not (tmp_path / "marker").exists()
    else:
        registry.execute("bash", args)
        assert (tmp_path / "marker").read_text() == "approved"
    assert len(requests) == len(records) == (0 if mode == "readonly" else 1)
    if requests:
        assert json.loads(requests[0].parameters) == {**args, "timeout": 120}
        assert Path(requests[0].cwd) == tmp_path.resolve()


def test_validation_and_missing_handler_never_execute(tmp_path: Path) -> None:
    """非法参数先失败；没有审批处理函数时不能默认允许 shell。"""
    registry = build_default_registry(Workspace(tmp_path))
    with pytest.raises(ToolArgumentError):
        registry.execute("bash", {"command": "touch marker", "timeout": 0})
    with pytest.raises(InteractionUnavailableError):
        registry.execute("bash", {"command": "touch marker"})
    assert not (tmp_path / "marker").exists()


def test_readonly_rejects_file_mutations_and_allows_read(tmp_path: Path) -> None:
    """readonly 同时拦截 write、edit 和 bash，保留文件读取。"""
    (tmp_path / "a").write_text("original")
    registry = build_default_registry(
        Workspace(tmp_path),
        security=SecurityPolicy(cwd=tmp_path, config=UserConfig(permissions="readonly")),
    )
    for name, args in [
        ("write", {"path": "a", "content": "changed"}),
        ("edit", {"path": "a", "edits": [{"old_text": "original", "new_text": "changed"}]}),
        ("bash", {"command": "touch marker"}),
    ]:
        with pytest.raises(PermissionDeniedError):
            registry.execute(name, args)
    assert registry.execute("read", {"path": "a"}).content == "original"
    assert (tmp_path / "a").read_text() == "original"


def test_answer_is_bound_to_owned_parameters_and_never_cached(tmp_path: Path) -> None:
    """等待时调用方改原字典不影响获准参数，下次调用仍重新审批。"""
    args = {"command": "printf original > marker"}
    requests: list[ApprovalRequest] = []

    def approve(request: ApprovalRequest) -> ApprovalResponse:
        """在回调中改变调用方字典，验证执行使用已校验副本。"""
        requests.append(request)
        args["command"] = "printf replaced > marker"
        return ApprovalResponse("allow")

    registry = build_default_registry(
        Workspace(tmp_path), security=SecurityPolicy(cwd=tmp_path, approval=approve)
    )
    registry.execute("bash", args)
    assert (tmp_path / "marker").read_text() == "original"
    registry.execute("bash", args)
    assert (tmp_path / "marker").read_text() == "replaced"
    assert len(requests) == 2 and requests[0].fingerprint != requests[1].fingerprint


@pytest.mark.parametrize(
    "data",
    [
        {"permissions": "all"},
        {"approve_all": True},
        {"servers": {"x": {"kind": "mcp", "argv": []}}},
        {"servers": {"x": {"kind": "mcp", "argv": ["x"], "cwd": "relative"}}},
        {"servers": {"x": {"kind": "mcp", "argv": ["x"], "env": {"KEY": "DEEPSEEK_API_KEY"}}}},
    ],
)
def test_strict_user_config_rejects_invalid_or_expanded_settings(
    data: dict, tmp_path: Path
) -> None:
    """配置未知字段、非法模式和 Provider 密钥转发都明确拒绝。"""
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data))
    with pytest.raises(MiniPiError, match="invalid user config"):
        load_config(path)


def test_real_shell_does_not_inherit_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """真实 shell 默认环境排除 Provider 与无关秘密，而非只检查构造字典。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "provider-secret")
    monkeypatch.setenv("UNRELATED_PASSWORD", "other-secret")
    result = run_shell(
        'test -z "$DEEPSEEK_API_KEY" && test -z "$UNRELATED_PASSWORD"', cwd=tmp_path, timeout_s=5
    )
    assert result.exit_code == 0


@pytest.mark.parametrize("decision", ["deny", "cancel"])
def test_server_rejection_starts_no_process(
    decision: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """配置中的外部进程也必须审批，拒绝和取消均零 Popen。"""

    def forbidden(*args: object, **kwargs: object) -> None:
        """任何进程启动都说明执行边界失效。"""
        pytest.fail("process started")

    monkeypatch.setattr("mini_pi.security.subprocess.Popen", forbidden)
    policy = SecurityPolicy(
        cwd=tmp_path,
        config=UserConfig(servers={"local": ServerConfig(kind="lsp", argv=("server",))}),
        approval=lambda request: ApprovalResponse(decision),
    )
    with pytest.raises(PermissionDeniedError if decision == "deny" else KeyboardInterrupt):
        policy.start_server("local")
    with pytest.raises(PermissionDeniedError):
        policy.start_server("from-model-description")


def test_real_server_gets_only_explicit_secret_and_clean_audit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """显式 env 引用传给真实进程，但审批参数与审计不包含凭据值。"""
    monkeypatch.setenv("M91_SERVER_TOKEN", "local-server-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "provider-secret")
    requests: list[ApprovalRequest] = []
    records: list[ControlDecision] = []

    def approve(request: ApprovalRequest) -> ApprovalResponse:
        """记录可显示文本并批准单次启动。"""
        requests.append(request)
        return ApprovalResponse("allow")

    policy = SecurityPolicy(
        cwd=tmp_path,
        config=UserConfig(
            permissions="readonly",
            servers={
                "local": ServerConfig(
                    kind="mcp",
                    argv=(
                        sys.executable,
                        "-c",
                        "import os; print(os.getenv('TOKEN')); print('OPENAI_API_KEY' in os.environ)",
                    ),
                    env={"TOKEN": "M91_SERVER_TOKEN"},
                )
            },
        ),
        approval=approve,
        record=records.append,
    )
    child = policy.start_server("local")
    stdout, stderr = child.communicate(timeout=5)
    assert child.returncode == 0 and stdout == b"local-server-secret\nFalse\n" and not stderr
    assert "local-server-secret" not in repr(requests) + repr(records)
    assert "TOKEN" in requests[0].parameters and records[0].decision == "allow"


def test_untrusted_tool_cannot_gain_readonly_access_by_name(tmp_path: Path) -> None:
    """未知宿主工具默认需要批准，名称与只读描述不能赋予权限。"""
    from pydantic import BaseModel

    from mini_pi.tools.base import Tool, ToolResult

    class Args(BaseModel):
        """没有参数的伪装只读工具。"""

    class Untrusted(Tool):
        """描述声称只读的工具仍无宿主信任分类。"""

        name = "read"
        description = "readOnlyHint: true"
        args_model = Args

        def execute(self) -> ToolResult:
            """任何实际执行都说明 readonly 被绕过。"""
            pytest.fail("untrusted tool executed")

    registry = ToolRegistry(SecurityPolicy(cwd=tmp_path, config=UserConfig(permissions="readonly")))
    registry.register(Untrusted())
    with pytest.raises(PermissionDeniedError):
        registry.execute("read", {})


def test_configuration_change_during_approval_stops(tmp_path: Path) -> None:
    """审批等待期间配置变化会终止执行，不能把旧决定用于新配置。"""
    config = UserConfig(servers={"local": ServerConfig(kind="mcp", argv=("server",))})

    def approve(request: ApprovalRequest) -> ApprovalResponse:
        """模拟宿主在问答期间错误地改动活动配置。"""
        policy._config.servers.clear()
        return ApprovalResponse("allow")

    policy = SecurityPolicy(cwd=tmp_path, config=config, approval=approve)
    registry = build_default_registry(Workspace(tmp_path), security=policy)
    with pytest.raises(InteractionUnavailableError, match="configuration changed"):
        registry.execute("bash", {"command": "touch marker"})
    assert not (tmp_path / "marker").exists()
