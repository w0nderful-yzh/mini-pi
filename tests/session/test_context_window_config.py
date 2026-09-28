"""M7.8.3：窗口配置在会话生命周期内保持同一份解析结果。"""

from __future__ import annotations

from pathlib import Path

import pytest

from mini_pi.context.policy import KNOWN_CONTEXT_WINDOWS, ContextPolicy
from mini_pi.session.jsonl import JsonlSession
from mini_pi.session.models import CompactionEntry
from mini_pi.session.runtime import AgentSession
from mini_pi.tools.registry import ToolRegistry
from tests.conftest import FakeLLMClient, assistant


def _session(
    tmp_path: Path,
    *,
    model: str,
    context_window: int | None = None,
    reserve_tokens: int = 8_192,
    llm: FakeLLMClient | None = None,
) -> AgentSession:
    """创建不访问网络的会话，窗口输入与 CLI 构造路径一致。"""
    return AgentSession.create(
        cwd=tmp_path,
        llm=llm or FakeLLMClient([]),
        registry=ToolRegistry(),
        provider="deepseek",
        model=model,
        sessions_root=tmp_path / "sessions",
        context_window=context_window,
        reserve_tokens=reserve_tokens,
    )


def test_custom_model_explicit_window_triggers_compaction(tmp_path: Path) -> None:
    """未知模型配置窗口后，下一任务按同一阈值先压缩再发送。"""
    llm = FakeLLMClient(
        [assistant("x" * 125_000), assistant("y" * 60_000), assistant("## Goal\n完成"), assistant("done")]
    )
    runtime = _session(tmp_path, model="my-custom-model", context_window=40_000, llm=llm)

    assert runtime.context_policy == ContextPolicy(context_window=40_000)
    runtime.run("first")
    runtime.run("second")
    result = runtime.run("third")

    assert result.content == "done"
    assert any(isinstance(entry, CompactionEntry) for entry in JsonlSession.load(runtime.path).entries)
    assert len(llm.calls) == 4  # 两次任务、一次摘要、最后一次任务。


def test_model_switch_recomputes_builtin_but_preserves_override(tmp_path: Path) -> None:
    """内置窗口随模型变，显式窗口跨模型切换保持优先。"""
    runtime = _session(tmp_path, model="deepseek-flash")
    assert runtime.context_policy == ContextPolicy(context_window=1_000_000)
    runtime.set_llm(FakeLLMClient([]), provider="openai", model="gpt-5.6-terra")
    assert runtime.context_policy == ContextPolicy(context_window=1_050_000)
    runtime.set_llm(FakeLLMClient([]), provider="openai", model="unknown-model")
    assert runtime.context_policy is None

    explicit = _session(tmp_path, model="unknown-model", context_window=32_000, reserve_tokens=2_000)
    explicit.set_llm(FakeLLMClient([]), provider="deepseek", model="deepseek-flash")
    assert explicit.context_policy == ContextPolicy(context_window=32_000, reserve_tokens=2_000)
    assert explicit.new().context_policy == explicit.context_policy


def test_invalid_model_switch_keeps_previous_policy(tmp_path: Path) -> None:
    """新模型窗口小于当前 reserve 时，切换失败且旧客户端元数据不变。"""
    runtime = _session(tmp_path, model="gpt-5.6-terra", reserve_tokens=1_020_000)
    before = runtime.context_policy

    with pytest.raises(ValueError, match="reserve_tokens"):
        runtime.set_llm(FakeLLMClient([]), provider="deepseek", model="deepseek-flash")

    assert runtime.model == "gpt-5.6-terra"
    assert runtime.context_policy is before


def test_resume_uses_current_cli_window_without_persisting_it(tmp_path: Path) -> None:
    """恢复时窗口来自本次运行参数，JSONL 不记录派生策略。"""
    original = _session(tmp_path, model="unknown-model", context_window=35_000)
    path = original.path
    assert "context_window" not in path.read_text(encoding="utf-8")

    default = AgentSession.resume(
        path, cwd=tmp_path, registry=ToolRegistry(),
        llm_factory=lambda provider, model: FakeLLMClient([]),
    )
    explicit = AgentSession.resume(
        path, cwd=tmp_path, registry=ToolRegistry(),
        llm_factory=lambda provider, model: FakeLLMClient([]),
        context_window=30_000, reserve_tokens=1_000,
    )
    assert default.context_policy is None
    assert explicit.context_policy == ContextPolicy(context_window=30_000, reserve_tokens=1_000)
    assert explicit.new().context_policy == explicit.context_policy


def test_invalid_window_fails_before_creating_session(tmp_path: Path) -> None:
    """无效 reserve 与窗口组合不能留下空 JSONL。"""
    with pytest.raises(ValueError, match="reserve_tokens"):
        _session(tmp_path, model="unknown-model", context_window=1_000)
    assert not (tmp_path / "sessions").exists()


def test_policy_is_frozen_for_current_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """构造后修改内置表也不让展示与决策读到不同窗口。"""
    runtime = _session(tmp_path, model="deepseek-flash")
    monkeypatch.setitem(KNOWN_CONTEXT_WINDOWS, "deepseek-flash", 40_000)
    assert runtime.context_policy == ContextPolicy(context_window=1_000_000)
