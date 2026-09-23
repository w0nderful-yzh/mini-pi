"""M7.6f：旧工具结果提前压缩的触发条件、失败边界与前后成本记录。"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from mini_pi.context.cost import CostDecision
from mini_pi.context.projection import SUMMARY_TAG
from mini_pi.context.tokens import estimate_tokens
from mini_pi.errors import LLMError
from mini_pi.llm.types import AssistantMessage, Message, ToolMessage, UserMessage
from mini_pi.session.jsonl import JsonlSession
from mini_pi.session.models import CompactionEntry, MessageEntry, SessionEntry
from mini_pi.session.runtime import AgentSession
from mini_pi.tools.base import Tool, ToolResult
from mini_pi.tools.registry import ToolRegistry
from tests.conftest import FakeLLMClient, assistant, tool_call

# 1M 窗口模型：窗口触发不会介入，夹具只观察成本触发
_WIDE_MODEL = "deepseek-flash"

# 约 25000 / 15000 token 的工具输出：region 由第一条工具轮主导
_HUGE_OUTPUT = "x" * 100_000
_BIG_OUTPUT = "y" * 60_000
# 约 25000 token 的助手正文：region 由普通对话主导，工具结果占比极低
_LONG_TEXT = "t" * 100_000


class _DumpArgs(BaseModel):
    """无参数工具的参数模型。"""


class SizedTool(Tool):
    """按调用序号返回不同长度的输出；计数执行次数以证明工具不重放。"""

    name = "dump"
    description = "Return a scripted output."
    args_model = _DumpArgs

    def __init__(self, *outputs: str) -> None:
        """固定每次调用的输出，并从 0 开始计数。"""
        self._outputs = list(outputs) or [""]
        self.calls = 0

    def execute(self) -> ToolResult:
        """返回本次调用的输出；超出脚本范围时重复最后一项。"""
        index = min(self.calls, len(self._outputs) - 1)
        self.calls += 1
        return ToolResult(content=self._outputs[index])


def _session(
    tmp_path: Path, llm: FakeLLMClient, tool: SizedTool, *, model: str = _WIDE_MODEL
) -> AgentSession:
    """创建持久会话；窗口足够大，只有成本触发可能介入。"""
    registry = ToolRegistry()
    registry.register(tool)
    return AgentSession.create(
        cwd=tmp_path,
        llm=llm,
        registry=registry,
        provider="deepseek",
        model=model,
        sessions_root=tmp_path / "sessions",
    )


def _entries(runtime: AgentSession) -> tuple[SessionEntry, ...]:
    """重新加载 JSONL，按磁盘事实断言。"""
    return JsonlSession.load(runtime.path).entries


def _compactions(runtime: AgentSession) -> list[CompactionEntry]:
    """活动链上的 compaction entry。"""
    return [entry for entry in _entries(runtime) if isinstance(entry, CompactionEntry)]


def _tool_contents(runtime: AgentSession) -> list[str]:
    """活动链上的工具结果正文；只出现一次即工具未重放。"""
    return [
        entry.message.content
        for entry in _entries(runtime)
        if isinstance(entry, MessageEntry) and isinstance(entry.message, ToolMessage)
    ]


def _request_tokens(calls: list[list[Message]]) -> int:
    """本次会话所有请求的输入估算之和，含摘要调用；离线口径，非 Provider 实测。"""
    return sum(estimate_tokens(messages).tokens for messages in calls)


def _two_round_script(second_round: AssistantMessage | LLMError) -> list:
    """脚本：第一轮工具 + 收尾，第二轮工具 + 成本触发的摘要 + 最终回答。"""
    return [
        assistant(tool_calls=[tool_call("c1", "dump", {})]),
        assistant("done"),
        assistant(tool_calls=[tool_call("c2", "dump", {})]),
        second_round,
        assistant("final answer"),
    ]


def test_early_compaction_fires_when_old_tool_results_dominate(
    tmp_path: Path,
) -> None:
    """旧工具结果主导且净收益为正时提前压缩：旧输出不再随请求重发。"""
    tool = SizedTool(_HUGE_OUTPUT, _BIG_OUTPUT)
    llm = FakeLLMClient(_two_round_script(assistant("## Goal\n整理日志")))
    runtime = _session(tmp_path, llm, tool)
    runtime.run("task")

    reply = runtime.run("second")

    assert reply.content == "final answer"
    assert tool.calls == 2
    assert _tool_contents(runtime) == [_HUGE_OUTPUT, _BIG_OUTPUT]
    compactions = _compactions(runtime)
    assert len(compactions) == 1
    # 切点落在第一条工具轮之后：被摘要的是旧工具轮，当前工具轮原样保留
    entries = _entries(runtime)
    assert compactions[0].first_kept_entry_id == entries[4].id
    assert _tool_contents(runtime) == [_HUGE_OUTPUT, _BIG_OUTPUT]
    # 调用序列：两次任务请求 → 带工具的第二轮请求 → 摘要（无工具）→ 压缩后的继续请求
    assert [tools is None for tools in llm.tools_seen] == [False, False, False, True, False]
    summary_request = llm.calls[3][1]
    assert isinstance(summary_request, UserMessage)
    assert "[User]: task" in summary_request.content
    assert "more characters truncated" in summary_request.content
    assert "second" not in summary_request.content
    # 继续请求只带当前工具轮：旧工具输出已被摘要替换
    continuation = llm.calls[4]
    assert isinstance(continuation[1], UserMessage) and SUMMARY_TAG in continuation[1].content
    kept_tools = [message for message in continuation if isinstance(message, ToolMessage)]
    assert [message.tool_call_id for message in kept_tools] == ["c2"]
    assert runtime.state.messages == list(JsonlSession.load(runtime.path).replay().messages)


def test_early_compaction_skipped_when_region_is_conversation_heavy(
    tmp_path: Path,
) -> None:
    """region 由助手正文主导时不提前压缩：收益不来自旧工具结果。"""
    tool = SizedTool("tiny", "tiny")
    llm = FakeLLMClient(
        [
            assistant(_LONG_TEXT, tool_calls=[tool_call("c1", "dump", {})]),
            assistant("done"),
            assistant(tool_calls=[tool_call("c2", "dump", {})]),
            assistant("final answer"),
        ]
    )
    runtime = _session(tmp_path, llm, tool)
    runtime.run("task")

    reply = runtime.run("second")

    assert reply.content == "final answer"
    assert _compactions(runtime) == []
    assert tool.calls == 2
    # 四次请求都是任务请求：工具轮之后没有额外的摘要调用
    assert len(llm.calls) == 4
    assert all(tools is not None for tools in llm.tools_seen)


def test_early_compaction_skipped_for_short_history(tmp_path: Path) -> None:
    """历史很短时连切点都没有：不调用摘要模型，也不留任何痕迹。"""
    tool = SizedTool("a" * 400, "a" * 400)
    llm = FakeLLMClient(
        [
            assistant(tool_calls=[tool_call("c1", "dump", {})]),
            assistant("done"),
            assistant(tool_calls=[tool_call("c2", "dump", {})]),
            assistant("final answer"),
        ]
    )
    runtime = _session(tmp_path, llm, tool)
    runtime.run("task")

    reply = runtime.run("second")

    assert reply.content == "final answer"
    assert _compactions(runtime) == []
    assert len(llm.calls) == 4


def test_early_compaction_skipped_for_unknown_window(tmp_path: Path) -> None:
    """窗口未知时不启用任何自动压缩，成本触发也不例外。"""
    tool = SizedTool(_HUGE_OUTPUT, _BIG_OUTPUT)
    llm = FakeLLMClient(
        [
            assistant(tool_calls=[tool_call("c1", "dump", {})]),
            assistant("done"),
            assistant(tool_calls=[tool_call("c2", "dump", {})]),
            assistant("final answer"),
        ]
    )
    runtime = _session(tmp_path, llm, tool, model="mystery-model")
    runtime.run("task")

    reply = runtime.run("second")

    assert reply.content == "final answer"
    assert _compactions(runtime) == []
    assert len(llm.calls) == 4


def test_summary_failure_keeps_run_and_stops_retrying(tmp_path: Path) -> None:
    """提前压缩失败不终止 run：保留原投影继续，且本次 run 不再重复尝试。"""
    tool = SizedTool(_HUGE_OUTPUT, _BIG_OUTPUT, _BIG_OUTPUT)
    llm = FakeLLMClient(
        [
            assistant(tool_calls=[tool_call("c1", "dump", {})]),
            assistant("done"),
            assistant(tool_calls=[tool_call("c2", "dump", {})]),
            LLMError("summary down", retryable=False),
            assistant(tool_calls=[tool_call("c3", "dump", {})]),
            assistant("final answer"),
            assistant("should never run"),
        ]
    )
    runtime = _session(tmp_path, llm, tool)
    runtime.run("task")

    reply = runtime.run("second")

    # 窗口内继续请求是安全的：run 正常结束，不是 agent error
    assert reply.stop_reason == "stop"
    assert reply.content == "final answer"
    assert tool.calls == 3
    assert _compactions(runtime) == []
    # 整场只有一次无工具调用：第一个工具轮没有切点，第三个工具轮因已尝试过而跳过
    assert [tools is None for tools in llm.tools_seen] == [
        False,
        False,
        False,
        True,
        False,
        False,
    ]
    assert runtime.state.messages == list(JsonlSession.load(runtime.path).replay().messages)


def test_cost_compaction_write_failure_stops_the_run(tmp_path: Path) -> None:
    """写盘失败不是“优化失败”：与窗口触发一样以 agent error 终止，避免投影分叉。"""
    tool = SizedTool(_HUGE_OUTPUT, _BIG_OUTPUT)
    llm = FakeLLMClient(_two_round_script(assistant("## Goal\n整理日志")))
    runtime = _session(tmp_path, llm, tool)
    runtime.run("task")
    before_messages = runtime.state.messages[:]

    def fail_append(self: JsonlSession, **kwargs: object) -> None:
        """用 OSError 模拟磁盘写失败；签名与 append_compaction 的关键字参数一致。"""
        raise OSError("disk full")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(JsonlSession, "append_compaction", fail_append)
        reply = runtime.run("second")

    assert reply.stop_reason == "error"
    assert reply.error_message is not None
    assert "automatic compaction failed" in reply.error_message
    assert "disk full" in reply.error_message
    # 摘要调用（第 4 次）之后没有继续请求；投影保留到失败点为止，没有摘要替换
    assert len(llm.calls) == 4
    assert llm.tools_seen[3] is None
    assert _compactions(runtime) == []
    assert not any(
        isinstance(message, UserMessage) and SUMMARY_TAG in message.content
        for message in runtime.state.messages
    )
    # 内存投影仍等于磁盘事实：写盘失败没有留下分叉
    assert runtime.state.messages == list(JsonlSession.load(runtime.path).replay().messages)
    assert len(runtime.state.messages) > len(before_messages)


def test_cost_aware_compaction_reduces_total_request_input(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """前后成本记录：同一脚本下，提前压缩把累计请求输入估算降低。

    两个分支只差成本决策：把决策强制为“不压缩”即 M7.6f 之前的窗口内行为
    （两个夹具的窗口都是 1M，窗口触发在两边都不会介入）。
    """
    before_dir = tmp_path / "before"
    after_dir = tmp_path / "after"
    before_dir.mkdir()
    after_dir.mkdir()
    script = _two_round_script(assistant("## Goal\n整理日志"))

    baseline_tool = SizedTool(_HUGE_OUTPUT, _BIG_OUTPUT)
    baseline_llm = FakeLLMClient(list(script))
    baseline = _session(before_dir, baseline_llm, baseline_tool)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            "mini_pi.session.runtime.decide_cost_aware_compaction",
            lambda cost, footprint: CostDecision(
                should_compact=False, reason="disabled for the baseline"
            ),
        )
        baseline.run("task")
        baseline.run("second")

    aware_tool = SizedTool(_HUGE_OUTPUT, _BIG_OUTPUT)
    aware_llm = FakeLLMClient(list(script))
    aware = _session(after_dir, aware_llm, aware_tool)
    aware.run("task")
    aware.run("second")

    before_tokens = _request_tokens(baseline_llm.calls)
    after_tokens = _request_tokens(aware_llm.calls)
    assert _compactions(baseline) == []
    assert len(_compactions(aware)) == 1
    # 记录行是这条用例的产出：离线估算口径，不得当成 Provider 实测计费
    record = (
        f"cost-aware: before={before_tokens} after={after_tokens} "
        f"saved={before_tokens - after_tokens} "
        f"requests_before={len(baseline_llm.calls)} requests_after={len(aware_llm.calls)}"
    )
    print(record)
    captured = capsys.readouterr().out.strip()
    fields = dict(item.split("=") for item in captured.removeprefix("cost-aware: ").split())
    assert int(fields["saved"]) > 0
    assert int(fields["after"]) == after_tokens
    assert int(fields["requests_after"]) == len(aware_llm.calls) == len(baseline_llm.calls) + 1
