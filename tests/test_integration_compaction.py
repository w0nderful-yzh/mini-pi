"""M7.7b：真实 Provider 上验证“压缩后继续跑”的会话闭环。

标记为 `integration`，默认被 `addopts = -m 'not integration'` 排除；只有显式
`uv run pytest -m integration tests/test_integration_compaction.py` 且配置了 Key 时才执行。
用例不输出任何凭据，Session 写入 tmp_path，不触碰用户真实 `~/.mini-pi`。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from mini_pi.auth import resolve_api_key
from mini_pi.cli.app import create_llm
from mini_pi.context.policy import DEFAULT_KEEP_RECENT_TOKENS
from mini_pi.context.tokens import estimate_message_tokens
from mini_pi.llm.types import AssistantMessage, Message, UserMessage
from mini_pi.session.jsonl import JsonlSession
from mini_pi.session.runtime import AgentSession
from mini_pi.tools import build_default_registry
from mini_pi.workspace.workspace import Workspace

pytestmark = pytest.mark.integration

# 工作区里的大输出脚本：单次 bash 结果会被工具层截到 50KB / 2000 行，约 12k token
_BIG_SCRIPT = "big.py"
_BIG_SCRIPT_BODY = 'print("\\n".join("z" * 200 for _ in range(2000)))\n'


def _configured_provider() -> tuple[str, str] | None:
    """返回第一个已配置 Key 的 provider 及其默认模型；都没有时返回 None。"""
    deepseek_key = resolve_api_key("deepseek", env_var="DEEPSEEK_API_KEY")
    if deepseek_key:
        return "deepseek", os.environ.get("MINI_PI_DEEPSEEK_MODEL", "deepseek-flash")
    openai_key = resolve_api_key("openai", env_var="OPENAI_API_KEY")
    if openai_key:
        return "openai", os.environ.get("MINI_PI_OPENAI_MODEL", "gpt-5.6-terra")
    return None


@pytest.mark.skipif(_configured_provider() is None, reason="no LLM API key configured")
def test_real_session_compacts_then_continues(tmp_path: Path) -> None:
    """真实模型跑多个工具轮 → 手动压缩 → 同会话继续 → 恢复投影一致。"""
    credentials = _configured_provider()
    assert credentials is not None
    provider, model = credentials
    workspace = Workspace(tmp_path)
    # 真实写入工作区的大输出脚本：让工具轮产生有界但足够大的 observation
    workspace.write_text(_BIG_SCRIPT, _BIG_SCRIPT_BODY)
    session = AgentSession.create(
        cwd=workspace.root,
        llm=create_llm(provider, model),
        registry=build_default_registry(workspace),
        provider=provider,
        model=model,
        sessions_root=tmp_path / "sessions",
        max_steps=6,
    )

    # 第一轮：让模型真实执行两次大输出命令，把活动投影推到保留预算之上
    session.run(
        "Use the bash tool twice, each time running exactly `python3 big.py`. "
        "Then reply with exactly: done"
    )
    entries_before = len(JsonlSession.load(session.path).entries)
    messages_before = list(session.state.messages)
    # 投影按同一字符规则比较：usage 锚点描述的是压缩前那次真实请求，无法反映压缩效果
    content_before = sum(estimate_message_tokens(message) for message in messages_before)
    assert any(isinstance(message, UserMessage) for message in messages_before)

    execution = session.compact(keep_recent_tokens=DEFAULT_KEEP_RECENT_TOKENS)
    if execution.result is None:
        # 模型可能只调了一次工具：用更小的保留预算再压一次，仍然验证同一事务
        execution = session.compact(keep_recent_tokens=2_000)
    assert execution.result is not None, execution.reason

    loaded = JsonlSession.load(session.path)
    # append-only：压缩只追加 CompactionEntry，原始 message entry 一条不删
    assert len(loaded.entries) == entries_before + 1
    content_after = sum(
        estimate_message_tokens(message) for message in session.state.messages
    )
    assert content_after < content_before
    assert execution.result.usage is not None

    # 恢复后投影与内存一致，压缩事实可重建
    resumed = AgentSession.resume(
        session.path,
        registry=build_default_registry(workspace),
        cwd=workspace.root,
        llm_factory=create_llm,
        max_steps=6,
    )
    assert [message.role for message in resumed.state.messages] == [
        message.role for message in session.state.messages
    ]
    assert sum(
        estimate_message_tokens(message) for message in resumed.state.messages
    ) == content_after

    # 压缩后的同一条链继续真实请求，验证“后续跑”
    answer = resumed.run("Reply with exactly: continued")
    assert isinstance(answer, AssistantMessage)
    assert answer.stop_reason == "stop"
    assert "continued" in answer.content.lower()

    # 仅用于人工验收记录；pytest 默认捕获，不污染常规输出，也不含任何凭据
    usage = execution.result.usage
    print(
        "[m7.7b] provider/model:", provider, model,
        "| entries_before:", entries_before,
        "| messages:", len(messages_before), "->", len(session.state.messages),
        "| content_tokens:", content_before, "->", content_after,
        "| summary_chars:", len(execution.result.summary),
        "| tokens_before(provider):", execution.result.plan.tokens_before.tokens,
        "| summary_usage:", None if usage is None else (usage.input_tokens, usage.output_tokens),
        "| continuation_chars:", len(answer.content),
    )


@pytest.mark.skipif(_configured_provider() is None, reason="no LLM API key configured")
def test_real_compaction_keeps_tool_pairing(tmp_path: Path) -> None:
    """真实压缩后 tool call/result 仍成对，且工具不因压缩被重放。"""
    credentials = _configured_provider()
    assert credentials is not None
    provider, model = credentials
    workspace = Workspace(tmp_path)
    workspace.write_text(_BIG_SCRIPT, _BIG_SCRIPT_BODY)
    session = AgentSession.create(
        cwd=workspace.root,
        llm=create_llm(provider, model),
        registry=build_default_registry(workspace),
        provider=provider,
        model=model,
        sessions_root=tmp_path / "sessions",
        max_steps=6,
    )
    session.run(
        "Use the bash tool once with exactly `python3 big.py`, then reply with exactly: done"
    )
    tool_messages_before = [
        message for message in session.state.messages if message.role == "tool"
    ]
    assert tool_messages_before, "model did not use the bash tool"

    execution = session.compact(keep_recent_tokens=2_000)
    assert execution.result is not None, execution.reason
    second = session.compact(keep_recent_tokens=2_000)
    # 再次压缩要么产生新检查点，要么明确说明没有新内容可摘要；两者都不改写原始消息
    lowered = second.reason.lower()
    assert second.result is not None or "no new" in lowered or "no safe cut point" in lowered

    assert _tool_pairing(session.state.messages) is None
    tool_messages_after = [
        message for message in session.state.messages if message.role == "tool"
    ]
    # 投影可能只保留最近工具结果，但保留的 ToolMessage 不能多出重复执行
    assert len(tool_messages_after) <= len(tool_messages_before)


def _tool_pairing(messages: list[Message]) -> str | None:
    """校验投影中每个 tool_call 都有对应结果；返回 None 表示通过，否则是原因。"""
    pending: list[str] = []
    for message in messages:
        if message.role == "assistant":
            pending.extend(call.id for call in message.tool_calls)
        elif message.role == "tool":
            if message.tool_call_id not in pending:
                return f"orphan tool result: {message.tool_call_id}"
            pending.remove(message.tool_call_id)
    if pending:
        return f"unpaired tool calls: {pending}"
    return None
