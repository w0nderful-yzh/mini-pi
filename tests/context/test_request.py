"""下一次模型请求的消息与工具必须按同一快照估算。"""

from __future__ import annotations

from pathlib import Path

from mini_pi.context.request import estimate_request
from mini_pi.context.stats import context_stats
from mini_pi.llm.types import (
    AssistantMessage,
    SectionPatch,
    SystemMessage,
    ToolSchema,
    Usage,
    UserMessage,
)
from mini_pi.session.runtime import AgentSession
from mini_pi.tools.registry import ToolRegistry
from tests.conftest import FakeLLMClient


def _tool(description: str = "Read a file") -> ToolSchema:
    """构造可辨识的函数 schema，便于验证工具成本。"""
    return ToolSchema(
        name="read",
        description=description,
        parameters={"type": "object", "properties": {"path": {"type": "string"}}},
    )


def test_request_replays_system_patch_and_counts_current_tool_schema() -> None:
    """历史 patch 按最终 system 回放，工具开销按当前函数 schema 计。"""
    previous = SystemMessage(sections={"preamble": "old rules" * 100, "tools": "read"})
    patch = SystemMessage(
        section_patch=[SectionPatch(op="set", id="preamble", content="new rules")]
    )
    user = UserMessage(content="task")
    tools = [_tool()]

    actual = estimate_request([previous, patch, user], tools, provider="openai")
    equivalent = estimate_request(
        [SystemMessage(sections={"preamble": "new rules", "tools": "read"}), user],
        tools,
        provider="openai",
    )
    without_tools = estimate_request([previous, patch, user], [], provider="openai")

    assert actual.input_tokens == equivalent.input_tokens
    assert actual.input_tokens > without_tools.input_tokens
    assert actual.source == "estimated"
    assert actual.provider == "openai"


def test_request_does_not_treat_previous_usage_as_current_measurement() -> None:
    """旧输出量不会抬高当前输入预测，DeepSeek 才回放 reasoning。"""
    assistant = AssistantMessage(
        content="done",
        reasoning_content="reasoning " * 100,
        usage=Usage(input_tokens=10, output_tokens=100_000, total_tokens=100_010),
    )
    messages = [UserMessage(content="task"), assistant]
    openai = estimate_request(messages, [], provider="openai")
    deepseek = estimate_request(messages, [], provider="deepseek")

    assert openai.input_tokens < 100_000
    assert deepseek.input_tokens > openai.input_tokens
    assert openai.source == deepseek.source == "estimated"


def test_request_snapshot_is_detached_from_mutable_input() -> None:
    """调用方随后修改消息或 schema，不得改变已预测的请求批次。"""
    user = UserMessage(content="before")
    tool = _tool()
    snapshot = estimate_request([user], [tool], model="custom")
    user.content = "after"
    tool.description = "changed"

    assert snapshot.messages[0].content == "before"
    assert snapshot.tools[0].description == "Read a file"
    assert snapshot.input_tokens == estimate_request(
        snapshot.messages, snapshot.tools, model="custom"
    ).input_tokens


def test_context_categories_include_tools_but_keep_their_own_total() -> None:
    """分类和请求预测同源但口径不同，分类总量只等于分量之和。"""
    messages = [UserMessage(content="task")]
    tools = [_tool()]
    stats = context_stats(messages, tools=tools)
    request = estimate_request(messages, tools)

    assert stats.tools > 0
    assert stats.total == (
        stats.system + stats.agents + stats.conversation + stats.tool_results
        + stats.summaries + stats.tools
    )
    assert request.input_tokens > stats.tools


def test_session_model_switch_recomputes_provider_specific_request(tmp_path: Path) -> None:
    """同一投影切到 DeepSeek 时，reasoning 回放与请求元数据同步改变。"""
    runtime = AgentSession.create(
        cwd=tmp_path,
        llm=FakeLLMClient([]),
        registry=ToolRegistry(),
        provider="openai",
        model="old-model",
        sessions_root=tmp_path / "sessions",
    )
    runtime.state.messages.append(
        AssistantMessage(content="answer", reasoning_content="reason " * 100)
    )
    before = runtime.request_snapshot()

    runtime.set_llm(FakeLLMClient([]), provider="deepseek", model="new-model")
    after = runtime.request_snapshot()

    assert (before.provider, before.model) == ("openai", "old-model")
    assert (after.provider, after.model) == ("deepseek", "new-model")
    assert after.input_tokens > before.input_tokens
