"""M7.4 总验收：投影 → 估算 → 策略 → 切点 的表驱动组合测试。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from mini_pi.context.compaction import find_cut_point
from mini_pi.context.policy import evaluate_compaction, resolve_policy
from mini_pi.context.projection import project_compaction, project_messages
from mini_pi.context.tokens import estimate_tokens
from mini_pi.llm.types import (
    AssistantMessage,
    Message,
    SystemMessage,
    ToolCall,
    ToolMessage,
    Usage,
    UserMessage,
)
from mini_pi.session.jsonl import JsonlSession

ProviderModel = tuple[str, str]
_DEFAULT_CONNECTION: ProviderModel = ("deepseek", "deepseek-flash")


def create_session(tmp_path: Path) -> JsonlSession:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return JsonlSession.create(
        cwd=workspace,
        provider=_DEFAULT_CONNECTION[0],
        model=_DEFAULT_CONNECTION[1],
        sessions_root=tmp_path / "sessions",
    )


def append(session: JsonlSession, message: Message):
    """追加一条消息并返回 entry，便于压缩切点引用。"""
    return session.append_message(
        message, provider=_DEFAULT_CONNECTION[0], model=_DEFAULT_CONNECTION[1]
    )


def projected(session: JsonlSession) -> tuple[Message, ...]:
    """统一投影：有 compaction 走 M7.4c，否则走 M7.4b。"""
    path = session.active_entries()
    compaction = project_compaction(path)
    if compaction is not None:
        return compaction.messages
    return project_messages(path).messages


def build_no_usage(tmp_path: Path) -> JsonlSession:
    session = create_session(tmp_path)
    append(session, UserMessage(content="aaaa"))
    append(session, AssistantMessage(content="bbbb"))
    return session


def build_trailing_messages(tmp_path: Path) -> JsonlSession:
    session = create_session(tmp_path)
    append(session, AssistantMessage(content="ok", usage=Usage(total_tokens=100)))
    append(session, UserMessage(content="abcdefgh"))
    return session


def build_consecutive_tool_calls(tmp_path: Path) -> JsonlSession:
    session = create_session(tmp_path)
    append(session, UserMessage(content="start"))
    append(
        session,
        AssistantMessage(
            tool_calls=[
                ToolCall(id="c1", name="read", arguments={"path": "a"}),
                ToolCall(id="c2", name="read", arguments={"path": "b"}),
            ]
        ),
    )
    append(session, ToolMessage(tool_call_id="c1", name="read", content="one"))
    append(session, ToolMessage(tool_call_id="c2", name="read", content="two"))
    append(session, AssistantMessage(content="done"))
    return session


def build_single_overlong_turn(tmp_path: Path) -> JsonlSession:
    session = create_session(tmp_path)
    append(session, UserMessage(content="x" * 4000))
    return session


def build_within_budget(tmp_path: Path) -> JsonlSession:
    session = create_session(tmp_path)
    append(session, UserMessage(content="aaaa"))
    append(session, AssistantMessage(content="bbbb"))
    return session


def build_repeated_compaction(tmp_path: Path) -> JsonlSession:
    session = create_session(tmp_path)
    first = append(session, UserMessage(content="first"))
    session.append_compaction(
        summary="first summary",
        first_kept_entry_id=first.id,
        tokens_before=10,
        system_message=SystemMessage(content="first system"),
    )
    second = append(session, UserMessage(content="second"))
    session.append_compaction(
        summary="second summary",
        first_kept_entry_id=second.id,
        tokens_before=20,
        system_message=SystemMessage(content="second system"),
    )
    append(session, AssistantMessage(content="tail"))
    return session


def verify_no_usage(session: JsonlSession) -> None:
    """无 usage：全部按字符规则估算，来源不得伪装成 provider。"""
    estimate = estimate_tokens(projected(session))
    decision = evaluate_compaction(estimate, policy=resolve_policy("deepseek-flash"))
    assert estimate.source == "estimated"
    assert decision.source == "estimated"
    assert decision.status in {"not_needed", "needed"}


def verify_trailing_messages(session: JsonlSession) -> None:
    """usage 之后还有消息：在其上追加估算。"""
    estimate = estimate_tokens(projected(session))
    assert estimate.source == "estimated"
    assert estimate.tokens == 102


def verify_consecutive_tool_calls(session: JsonlSession) -> None:
    """连续工具调用：投影保持配对，任何切点都不落在 tool message。"""
    messages = projected(session)
    assert [message.role for message in messages] == [
        "user",
        "assistant",
        "tool",
        "tool",
        "assistant",
    ]
    total = estimate_tokens(messages).tokens
    for budget in range(1, total + 2):
        cut = find_cut_point(messages, keep_recent_tokens=budget)
        if cut is None:
            continue
        start = messages[cut.start_index]
        assert start.role in {"user", "assistant"}
        if isinstance(start, AssistantMessage) and start.tool_calls:
            kept_ids = {
                message.tool_call_id
                for message in messages[cut.start_index :]
                if isinstance(message, ToolMessage)
            }
            assert {call.id for call in start.tool_calls} <= kept_ids


def verify_single_overlong_turn(session: JsonlSession) -> None:
    """单个超长 turn：找不到安全切点，不强行截断。"""
    assert find_cut_point(projected(session), keep_recent_tokens=10) is None


def verify_within_budget(session: JsonlSession) -> None:
    """全部消息在预算内：无需压缩。"""
    assert find_cut_point(projected(session), keep_recent_tokens=1000) is None


def verify_repeated_compaction(session: JsonlSession) -> None:
    """重复压缩：只使用最新一份快照、摘要与保留消息。"""
    projection = project_compaction(session.active_entries())
    assert projection is not None
    assert projection.system_prompt.content == "second system"
    assert "second summary" in projection.summary_message.content
    assert projection.kept_messages == (
        UserMessage(content="second"),
        AssistantMessage(content="tail"),
    )


Scenario = tuple[str, Callable[[Path], JsonlSession], Callable[[JsonlSession], None]]

SCENARIOS: tuple[Scenario, ...] = (
    ("no_usage", build_no_usage, verify_no_usage),
    ("trailing_messages", build_trailing_messages, verify_trailing_messages),
    ("consecutive_tool_calls", build_consecutive_tool_calls, verify_consecutive_tool_calls),
    ("single_overlong_turn", build_single_overlong_turn, verify_single_overlong_turn),
    ("within_budget", build_within_budget, verify_within_budget),
    ("repeated_compaction", build_repeated_compaction, verify_repeated_compaction),
)


@pytest.mark.parametrize(
    ("build", "verify"),
    [(build, verify) for _, build, verify in SCENARIOS],
    ids=[name for name, _, _ in SCENARIOS],
)
def test_m7_4_pipeline(
    build: Callable[[Path], JsonlSession],
    verify: Callable[[JsonlSession], None],
    tmp_path: Path,
) -> None:
    """表驱动验收：每个场景都要能从 Session 一路走到安全决策。"""
    verify(build(tmp_path))
