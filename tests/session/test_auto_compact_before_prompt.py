"""M7.6b：新 user 消息前的窗口检查与自动压缩测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from mini_pi.context.policy import KNOWN_CONTEXT_WINDOWS, ContextPolicy
from mini_pi.context.projection import SUMMARY_TAG
from mini_pi.context.tokens import estimate_tokens
from mini_pi.errors import LLMError
from mini_pi.llm.types import UserMessage
from mini_pi.session.jsonl import JsonlSession
from mini_pi.session.models import CompactionEntry, MessageEntry, SessionEntry
from mini_pi.session.runtime import AgentSession
from mini_pi.tools.registry import ToolRegistry
from tests.conftest import EchoTool, FakeLLMClient, assistant

# 小窗口模型：阈值 = 40000 - 8192 = 31808 token，大于默认保留预算 20000
_SMALL_WINDOW_MODEL = "test-small-window"
_SMALL_WINDOW = 40_000

# 约 35000 token 的回复：单条就超过阈值，同时超过保留预算
_HUGE_REPLY = "x" * 140_000


def _registry() -> ToolRegistry:
    """注册 echo，保证 Agent Loop 有可用工具但不参与压缩判断。"""
    registry = ToolRegistry()
    registry.register(EchoTool())
    return registry


def _session(tmp_path: Path, llm: FakeLLMClient, *, model: str) -> AgentSession:
    """创建持久会话；模型决定窗口策略是否生效。"""
    return AgentSession.create(
        cwd=tmp_path,
        llm=llm,
        registry=_registry(),
        provider="deepseek",
        model=model,
        sessions_root=tmp_path / "sessions",
    )


def _small_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """把测试模型登记进已知窗口表，走真实的 M7.4g 策略判定。"""
    monkeypatch.setitem(KNOWN_CONTEXT_WINDOWS, _SMALL_WINDOW_MODEL, _SMALL_WINDOW)


def _entries(runtime: AgentSession) -> tuple[SessionEntry, ...]:
    """重新加载 JSONL，按磁盘事实断言。"""
    return JsonlSession.load(runtime.path).entries


def _user_contents(runtime: AgentSession) -> list[str]:
    """活动路径上的 user 消息正文，用于确认只追加了一条。"""
    return [
        entry.message.content
        for entry in _entries(runtime)
        if isinstance(entry, MessageEntry) and isinstance(entry.message, UserMessage)
    ]


def _compactions(runtime: AgentSession) -> list[CompactionEntry]:
    """活动路径上的 compaction entry。"""
    return [entry for entry in _entries(runtime) if isinstance(entry, CompactionEntry)]


def test_below_threshold_appends_single_user_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """未超阈值时不压缩：没有摘要请求，user 消息只追加一条。"""
    _small_window(monkeypatch)
    llm = FakeLLMClient([assistant("done"), assistant("done again")])
    runtime = _session(tmp_path, llm, model=_SMALL_WINDOW_MODEL)
    runtime.run("task")

    runtime.run("second")

    assert _user_contents(runtime) == ["task", "second"]
    assert _compactions(runtime) == []
    # 两次 run 各一次请求：没有额外的摘要调用
    assert len(llm.calls) == 2


def test_above_threshold_compacts_before_appending_user_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """超阈值时先压缩再追加 user 消息：摘要不含新 prompt，原 entry 不删。"""
    _small_window(monkeypatch)
    llm = FakeLLMClient([assistant(_HUGE_REPLY), assistant("## Goal\n完成"), assistant("done")])
    runtime = _session(tmp_path, llm, model=_SMALL_WINDOW_MODEL)
    runtime.run("task")

    runtime.run("second")

    entries = _entries(runtime)
    assert [type(entry).__name__ for entry in entries] == [
        "MessageEntry",
        "MessageEntry",
        "MessageEntry",
        "CompactionEntry",
        "MessageEntry",
        "MessageEntry",
    ]
    assert _user_contents(runtime) == ["task", "second"]
    # 摘要请求只包含压缩前的历史，新 prompt 不在其中
    summary_request = llm.calls[1][1]
    assert isinstance(summary_request, UserMessage)
    assert "[User]: task" in summary_request.content
    assert "second" not in summary_request.content
    replayed = JsonlSession.load(runtime.path).replay().messages
    assert len(replayed) == 5
    assert isinstance(replayed[1], UserMessage) and SUMMARY_TAG in replayed[1].content
    assert isinstance(replayed[3], UserMessage) and replayed[3].content == "second"


def test_unknown_window_never_auto_compacts(tmp_path: Path) -> None:
    """窗口未知时不猜百分比，即使投影很大也不自动压缩。"""
    llm = FakeLLMClient([assistant(_HUGE_REPLY), assistant("done")])
    runtime = _session(tmp_path, llm, model="mystery-model")
    runtime.run("task")

    runtime.run("second")

    assert _compactions(runtime) == []
    assert len(llm.calls) == 2
    assert _user_contents(runtime) == ["task", "second"]


def test_one_compaction_per_prompt_even_when_still_over_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """每次 run 只检查一次：压缩后仍超阈值也不在同一轮里反复压缩。"""
    _small_window(monkeypatch)
    llm = FakeLLMClient([assistant(_HUGE_REPLY), assistant("## Goal\n完成"), assistant("done")])
    runtime = _session(tmp_path, llm, model=_SMALL_WINDOW_MODEL)
    runtime.run("task")

    runtime.run("second")

    assert len(_compactions(runtime)) == 1
    assert _user_contents(runtime) == ["task", "second"]
    assert len(llm.calls) == 3
    # 长回复落在保留区，压缩后投影依旧超过阈值；本轮不再触发第二次摘要
    policy = ContextPolicy(context_window=_SMALL_WINDOW)
    assert estimate_tokens(runtime.state.messages).tokens > policy.threshold_tokens


def test_summary_failure_keeps_session_and_skips_user_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """自动压缩失败时 run 直接失败：不追加 user 消息，也不留压缩痕迹。

    失败如何转成 agent error 由 M7.6d 定义；这里先固定“不写半成品”的边界。
    """
    _small_window(monkeypatch)
    llm = FakeLLMClient([assistant(_HUGE_REPLY), LLMError("summary down", retryable=False)])
    runtime = _session(tmp_path, llm, model=_SMALL_WINDOW_MODEL)
    runtime.run("task")
    before_file = runtime.path.read_text(encoding="utf-8")

    with pytest.raises(LLMError, match="summary down"):
        runtime.run("second")

    assert runtime.path.read_text(encoding="utf-8") == before_file
    assert _user_contents(runtime) == ["task"]
    assert _compactions(runtime) == []


def test_empty_task_is_rejected_before_any_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """空任务在压缩检查之前就报错，不产生任何 Session 写入与模型请求。"""
    _small_window(monkeypatch)
    llm = FakeLLMClient([assistant(_HUGE_REPLY)])
    runtime = _session(tmp_path, llm, model=_SMALL_WINDOW_MODEL)
    runtime.run("task")
    before = runtime.path.read_text(encoding="utf-8")

    with pytest.raises(ValueError, match="task must not be empty"):
        runtime.run("   ")

    assert runtime.path.read_text(encoding="utf-8") == before
    assert len(llm.calls) == 1


def test_fixture_truly_exceeds_policy_threshold() -> None:
    """触发线沿用 M7.4g（窗口减 reserve）：长回复按字符规则确实越过阈值。"""
    policy = ContextPolicy(context_window=_SMALL_WINDOW)

    assert policy.threshold_tokens == _SMALL_WINDOW - policy.reserve_tokens
    assert len(_HUGE_REPLY) // 4 > policy.threshold_tokens
