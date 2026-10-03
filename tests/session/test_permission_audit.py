"""审批事实的 durable-first、投影、压缩和恢复边界。"""

from __future__ import annotations

from pathlib import Path

import pytest

from mini_pi.context.compaction import prepare_compaction
from mini_pi.context.projection import project_messages
from mini_pi.errors import SessionError
from mini_pi.interaction import ApprovalResponse
from mini_pi.llm.types import ToolMessage
from mini_pi.security import SecurityPolicy
from mini_pi.session.jsonl import JsonlSession
from mini_pi.session.models import ControlEntry
from mini_pi.session.runtime import AgentSession
from mini_pi.tools import build_default_registry
from mini_pi.workspace.workspace import Workspace
from tests.conftest import FakeLLMClient, assistant, tool_call


def runtime(tmp_path: Path, decision: str = "allow") -> AgentSession:
    """装配真实 shell、JSONL 和离线模型，宿主显式给出本次决定。"""
    registry = build_default_registry(
        Workspace(tmp_path),
        security=SecurityPolicy(cwd=tmp_path, approval=lambda request: ApprovalResponse(decision)),
    )
    return AgentSession.create(
        cwd=tmp_path,
        llm=FakeLLMClient(
            [
                assistant(
                    tool_calls=[tool_call("c1", "bash", {"command": "printf approved > marker"})]
                ),
                assistant("done"),
            ]
        ),
        registry=registry,
        provider="deepseek",
        model="deepseek-flash",
        sessions_root=tmp_path / "sessions",
    )


@pytest.mark.parametrize("decision", ["allow", "deny"])
def test_audit_precedes_result_and_is_absent_from_messages(decision: str, tmp_path: Path) -> None:
    """新 metadata 位于真实工具配对之间，但不会成为模型消息或用量。"""
    agent = runtime(tmp_path, decision)
    agent.run("run shell")
    loaded = JsonlSession.load(agent.path)
    control = next(entry for entry in loaded.entries if isinstance(entry, ControlEntry))
    assert control.decision == decision
    at = loaded.entries.index(control)
    assert loaded.entries[at - 1].message.tool_calls[0].id == "c1"
    assert isinstance(loaded.entries[at + 1].message, ToolMessage)
    assert loaded.entries[at + 1].message.is_error == (decision == "deny")
    assert loaded.replay().messages == project_messages(loaded.active_entries()).messages
    assert agent.last_run_usage.tool_calls == 1 and agent.last_run_usage.requests == 2
    assert (tmp_path / "marker").exists() == (decision == "allow")


def test_audit_write_failure_prevents_side_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """审批写入失败时零执行，能写的配对错误 observation 保留。"""
    agent = runtime(tmp_path)

    def fail(decision: object) -> None:
        """仅阻断审批记录，模拟审核事实无法落盘。"""
        raise SessionError("disk failed")

    monkeypatch.setattr(agent._session, "append_control", fail)
    result = agent.run("run shell")
    assert result.stop_reason == "error"
    assert not (tmp_path / "marker").exists()
    assert JsonlSession.load(agent.path).replay().messages[-1].is_error


def test_resume_and_new_session_never_reuse_allow(tmp_path: Path) -> None:
    """当前宿主决定覆盖历史，旧 runtime 的审计绑定也不会被 new 改走。"""
    agent = runtime(tmp_path)
    agent.run("first")
    prefix = agent.path.read_bytes()
    (tmp_path / "marker").unlink()
    llm = FakeLLMClient(
        [
            assistant(tool_calls=[tool_call("c2", "bash", {"command": "touch marker"})]),
            assistant("denied"),
        ]
    )
    restored = AgentSession.resume(
        agent.path,
        cwd=tmp_path,
        llm_factory=lambda *args: llm,
        registry=build_default_registry(
            Workspace(tmp_path),
            security=SecurityPolicy(
                cwd=tmp_path, approval=lambda request: ApprovalResponse("deny")
            ),
        ),
    )
    restored.run("again")
    assert not (tmp_path / "marker").exists() and restored.path.read_bytes().startswith(prefix)
    controls = [
        entry for entry in JsonlSession.load(agent.path).entries if isinstance(entry, ControlEntry)
    ]
    assert [entry.decision for entry in controls] == ["allow", "deny"]
    new = restored.new(sessions_root=tmp_path / "new-sessions")
    new._llm._script = [
        assistant(tool_calls=[tool_call("n1", "bash", {"command": "touch marker"})]),
        assistant("denied"),
    ]
    new.run("new task")
    assert not (tmp_path / "marker").exists()
    assert new._registry is not restored._registry
    assert [
        entry.decision
        for entry in JsonlSession.load(new.path).entries
        if isinstance(entry, ControlEntry)
    ] == ["deny"]
    assert restored.path.read_bytes().startswith(prefix)


def test_compaction_preserves_audit_without_summarizing_it(tmp_path: Path) -> None:
    """审计留在原始文件和 parent 链，摘要正文与切点只处理消息。"""
    agent = runtime(tmp_path)
    agent.run("run shell")
    loaded = JsonlSession.load(agent.path)
    plan = prepare_compaction(loaded.active_entries(), keep_recent_tokens=5).plan
    assert plan is not None
    assert all("control" not in str(message.model_dump()) for message in plan.messages_to_summarize)
    prefix = agent.path.read_bytes()
    loaded.append_compaction(
        summary="Shell completed.",
        first_kept_entry_id=plan.first_kept_entry_id,
        tokens_before=plan.tokens_before.tokens,
        system_message=plan.system_message,
    )
    reloaded = JsonlSession.load(agent.path)
    assert agent.path.read_bytes().startswith(prefix)
    assert sum(isinstance(entry, ControlEntry) for entry in reloaded.entries) == 1
    assert reloaded.replay().messages


def test_multiple_calls_require_independent_decisions_and_cancel_remaining(tmp_path: Path) -> None:
    """同批逐个审批，第二个取消保留首个事实并为全部剩余调用补齐结果。"""
    decisions = iter(["allow", "cancel"])
    llm = FakeLLMClient(
        [
            assistant(
                tool_calls=[
                    tool_call("b1", "bash", {"command": "touch first"}),
                    tool_call("b2", "bash", {"command": "touch second"}),
                    tool_call("b3", "bash", {"command": "touch third"}),
                ]
            ),
            assistant("unused"),
        ]
    )
    agent = AgentSession.create(
        cwd=tmp_path,
        llm=llm,
        registry=build_default_registry(
            Workspace(tmp_path),
            security=SecurityPolicy(
                cwd=tmp_path, approval=lambda request: ApprovalResponse(next(decisions))
            ),
        ),
        provider="deepseek",
        model="deepseek-flash",
        sessions_root=tmp_path / "sessions",
    )
    assert agent.run("batch").stop_reason == "cancelled" and len(llm.calls) == 1
    assert (
        (tmp_path / "first").exists()
        and not (tmp_path / "second").exists()
        and not (tmp_path / "third").exists()
    )
    messages = JsonlSession.load(agent.path).replay().messages
    results = [message for message in messages if isinstance(message, ToolMessage)]
    assert [message.tool_call_id for message in results] == ["b1", "b2", "b3"]
    assert [message.is_error for message in results] == [False, True, True]
