"""AgentSession 跨实例恢复及配置失败边界测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from mini_pi.auth import save_api_key
from mini_pi.errors import MiniPiError, SessionError
from mini_pi.llm.types import AssistantMessage, SystemMessage, UserMessage
from mini_pi.session.jsonl import JsonlSession
from mini_pi.session.runtime import AgentSession
from mini_pi.tools.registry import ToolRegistry
from tests.conftest import EchoTool, FakeLLMClient, assistant, tool_call


def create_runtime(workspace: Path, sessions_root: Path, llm: FakeLLMClient) -> AgentSession:
    """使用离线 LLM 创建可持久化的测试会话。"""
    registry = ToolRegistry()
    registry.register(EchoTool())
    return AgentSession.create(
        cwd=workspace,
        llm=llm,
        registry=registry,
        provider="deepseek",
        model="deepseek-chat",
        sessions_root=sessions_root,
    )


def test_resume_restores_tool_history_and_continues_same_leaf(tmp_path: Path) -> None:
    """新实例读回完整工具轮，并沿原 leaf 追加下一轮消息。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first = create_runtime(
        workspace,
        tmp_path / "sessions",
        FakeLLMClient([
            assistant(tool_calls=[tool_call("c1", "echo", {"text": "hi"})]),
            assistant("first done"),
        ]),
    )
    first.run("echo hi")
    before = JsonlSession.load(first.path)
    original_leaf = before.leaf_id
    next_llm = FakeLLMClient([assistant("second done")])
    chosen: list[tuple[str, str]] = []

    def make_llm(provider: str, model: str) -> FakeLLMClient:
        """记录恢复时使用的 provider/model，不调用真实 API。"""
        chosen.append((provider, model))
        return next_llm

    resumed_registry = ToolRegistry()
    resumed_registry.register(EchoTool())
    resumed = AgentSession.resume(
        first.path,
        registry=resumed_registry,
        llm_factory=make_llm,
        cwd=workspace,
    )
    assert chosen == [("deepseek", "deepseek-chat")]
    assert resumed.state.messages == list(before.replay().messages)
    assert resumed.state.step_count == 2
    assert resumed.state.modified_files == {"echo/hi.txt"}

    result = resumed.run("continue")

    assert result.content == "second done"
    assert next_llm.calls[0] == list(before.replay().messages) + [UserMessage(content="continue")]
    after = JsonlSession.load(first.path)
    assert after.entries[: len(before.entries)] == before.entries
    assert after.entries[len(before.entries)].parent_id == original_leaf
    assert [entry.message.role for entry in after.entries[-2:]] == ["user", "assistant"]
    assert after.entries[-1].step_count == 3
    assert resumed.state.step_count == 3
    assert after.leaf_id == after.entries[-1].id


def test_resume_uses_last_entry_model_and_explicit_overrides(tmp_path: Path) -> None:
    """默认选活动路径最后的模型；显式覆盖只影响后续 entry。"""
    session = JsonlSession.create(
        cwd=tmp_path,
        provider="deepseek",
        model="old-model",
        sessions_root=tmp_path / "sessions",
    )
    session.append_message(
        SystemMessage(content="legacy"),
        provider="deepseek",
        model="old-model",
        step_count=0,
    )
    session.append_message(
        AssistantMessage(content="old answer"),
        provider="openai",
        model="new-model",
        step_count=1,
    )
    observed: list[tuple[str, str]] = []

    def make_llm(provider: str, model: str) -> FakeLLMClient:
        """校验构造参数，并提供本轮模型回复。"""
        observed.append((provider, model))
        return FakeLLMClient([assistant("continued")])

    resumed = AgentSession.resume(
        session.path, registry=ToolRegistry(), llm_factory=make_llm
    )
    assert observed == [("openai", "new-model")]
    resumed.run("task")
    loaded = JsonlSession.load(session.path)
    assert all(entry.provider == "openai" for entry in loaded.entries[2:])
    assert all(entry.model == "new-model" for entry in loaded.entries[2:])

    overridden = AgentSession.resume(
        session.path,
        registry=ToolRegistry(),
        llm_factory=make_llm,
        provider="deepseek",
        model="override-model",
    )
    assert observed[-1] == ("deepseek", "override-model")
    overridden.run("again")
    latest = JsonlSession.load(session.path)
    assert all(entry.provider == "deepseek" for entry in latest.entries[-2:])
    assert all(entry.model == "override-model" for entry in latest.entries[-2:])
    assert latest.header.model == "old-model"


def test_resume_rejects_missing_workspace(tmp_path: Path) -> None:
    """header 中的 cwd 已不存在时，在构造客户端前失败。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first = create_runtime(workspace, tmp_path / "sessions", FakeLLMClient([]))
    workspace.rename(tmp_path / "moved")
    called = False

    def make_llm(provider: str, model: str) -> FakeLLMClient:
        """若调用则表示 cwd 校验顺序错误。"""
        nonlocal called
        called = True
        return FakeLLMClient([])

    with pytest.raises(SessionError, match="cwd.*does not exist"):
        AgentSession.resume(first.path, registry=ToolRegistry(), llm_factory=make_llm)
    assert called is False


def test_resume_rejects_explicit_cwd_mismatch(tmp_path: Path) -> None:
    """显式 cwd 必须与 Session header 解析后相同。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    first = create_runtime(workspace, tmp_path / "sessions", FakeLLMClient([]))

    with pytest.raises(SessionError, match="cwd mismatch"):
        AgentSession.resume(
            first.path,
            registry=ToolRegistry(),
            llm_factory=lambda provider, model: FakeLLMClient([]),
            cwd=other,
        )


def test_resume_without_credentials_fails_before_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """默认客户端只从认证配置取 Key；缺失时不创建新 entry。"""
    first = create_runtime(tmp_path, tmp_path / "sessions", FakeLLMClient([]))
    before = JsonlSession.load(first.path)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr("mini_pi.auth.DEFAULT_AUTH_PATH", tmp_path / "missing-auth.json")

    with pytest.raises(MiniPiError, match="DEEPSEEK_API_KEY"):
        AgentSession.resume(first.path, registry=ToolRegistry())

    after = JsonlSession.load(first.path)
    assert after.entries == before.entries
    assert after.leaf_id == before.leaf_id


def test_resume_uses_saved_credentials_without_persisting_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """默认恢复可从 auth.json 构造客户端，Session 文件只保存模型元数据。"""
    first = create_runtime(tmp_path, tmp_path / "sessions", FakeLLMClient([]))
    auth_path = tmp_path / "auth.json"
    save_api_key("deepseek", "test-secret", path=auth_path)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr("mini_pi.auth.DEFAULT_AUTH_PATH", auth_path)

    resumed = AgentSession.resume(first.path, registry=ToolRegistry())

    assert resumed.path == first.path
    assert resumed.state.messages == []
    assert "test-secret" not in first.path.read_text(encoding="utf-8")


def test_resume_rejects_compaction_before_client_creation(tmp_path: Path) -> None:
    """M7.3d 不解释压缩 entry，不能跳过它继续运行。"""
    session = JsonlSession.create(
        cwd=tmp_path,
        provider="deepseek",
        model="deepseek-chat",
        sessions_root=tmp_path / "sessions",
    )
    first = session.append_message(
        UserMessage(content="old"), provider="deepseek", model="deepseek-chat"
    )
    session.append_compaction(
        summary="summary",
        first_kept_entry_id=first.id,
        tokens_before=100,
        system_message=SystemMessage(content="system"),
    )
    called = False

    def make_llm(provider: str, model: str) -> FakeLLMClient:
        """压缩检查应早于客户端构造。"""
        nonlocal called
        called = True
        return FakeLLMClient([])

    with pytest.raises(SessionError, match="compaction.*M7.4"):
        AgentSession.resume(session.path, registry=ToolRegistry(), llm_factory=make_llm)
    assert called is False
