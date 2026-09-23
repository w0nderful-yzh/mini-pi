"""M7.6f：长工具输出的生命周期——切点、摘要输入与成本模型。"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from mini_pi.context.compaction import prepare_compaction
from mini_pi.context.cost import (
    ASSUMED_REMAINING_REQUESTS,
    ASSUMED_SUMMARY_TOKENS,
    TOOL_RESULT_SHARE_FLOOR,
    decide_cost_aware_compaction,
    estimate_compaction_cost,
    measure_tool_results,
)
from mini_pi.context.serializer import TOOL_RESULT_LIMIT, serialize_transcript
from mini_pi.context.summarizer import (
    SUMMARIZATION_SYSTEM_PROMPT,
    build_summary_messages,
)
from mini_pi.llm.types import (
    AssistantMessage,
    Message,
    SystemMessage,
    ToolCall,
    ToolMessage,
    UserMessage,
)
from mini_pi.session.models import MessageEntry, SessionEntry

_TIMESTAMP = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)

# 真实形态的样本：构建/测试日志、git diff、搜索结果。关键事实都靠前，噪音在后。
_FILLER = "\n".join(f"[{index:04d}] collected 1 item / 1 deselected" for index in range(400))
_PYTEST_LOG = (
    "$ uv run pytest tests/test_calculator.py -q\n"
    "F...                                                                     [100%]\n"
    "=================================== FAILURES ===================================\n"
    "___________________________ test_divide_by_zero ____________________________\n"
    "\n"
    "    def test_divide_by_zero():\n"
    ">       assert divide(1, 0) == 0\n"
    "E       ZeroDivisionError: division by zero\n"
    "\n"
    "tests/test_calculator.py:12: ZeroDivisionError\n"
    "=========================== short test summary info ============================\n"
    "FAILED tests/test_calculator.py::test_divide_by_zero - ZeroDivisionError\n"
    "1 failed, 3 passed in 0.42s\n"
    "exit code 1\n" + _FILLER
)
_GIT_DIFF = (
    "diff --git a/src/calculator.py b/src/calculator.py\n"
    "index 1a2b3c4..5d6e7f8 100644\n"
    "--- a/src/calculator.py\n"
    "+++ b/src/calculator.py\n"
    "@@ -8,6 +8,9 @@ def divide(a, b):\n"
    "-    return a / b\n"
    "+    if b == 0:\n"
    '+        raise ValueError("division by zero")\n'
    "+    return a / b\n" + _FILLER
)
_SEARCH_OUTPUT = (
    "src/calculator.py:8:def divide(a, b):\n"
    "src/calculator.py:12:    return a / b\n"
    "tests/test_calculator.py:12:    assert divide(1, 0) == 0\n" + _FILLER
)
# 40 字符 = 10 token，用于构造可预测的短消息
_SMALL = 40


def append_message(
    path: list[SessionEntry], message: Message, *, entry_id: UUID | None = None
) -> MessageEntry:
    """在链尾追加 message entry，返回新建 entry 以便断言 id。"""
    entry = MessageEntry(
        type="message",
        id=entry_id or uuid4(),
        parent_id=path[-1].id if path else None,
        timestamp=_TIMESTAMP,
        provider="deepseek",
        model="deepseek-chat",
        message=message,
    )
    path.append(entry)
    return entry


def append_tool_round(
    path: list[SessionEntry],
    *,
    call_id: str,
    name: str,
    output: str,
    modified_files: list[str] | None = None,
) -> tuple[MessageEntry, MessageEntry]:
    """追加一次完整的工具轮（调用 + 结果），返回两个 entry。"""
    call = append_message(
        path,
        AssistantMessage(
            tool_calls=[ToolCall(id=call_id, name=name, arguments={"command": "uv run pytest"})]
        ),
    )
    result = append_message(
        path,
        ToolMessage(
            tool_call_id=call_id,
            name=name,
            content=output,
            modified_files=modified_files or [],
        ),
    )
    return call, result


def tool_heavy_path() -> list[SessionEntry]:
    """历史：一次长日志工具轮（可摘要）+ 当前收尾轮（必须保留）。"""
    path: list[SessionEntry] = []
    append_message(path, SystemMessage(sections={"preamble": "p"}))
    append_message(path, UserMessage(content="a" * _SMALL))
    append_tool_round(
        path,
        call_id="call_1",
        name="bash",
        output=_PYTEST_LOG,
        modified_files=["src/calculator.py"],
    )
    append_tool_round(path, call_id="call_2", name="git", output=_GIT_DIFF)
    append_message(path, UserMessage(content="c" * _SMALL))
    append_message(path, AssistantMessage(content="d" * _SMALL))
    return path


def test_serialized_long_tool_outputs_keep_failure_reason_files_and_diff() -> None:
    """摘要输入保留失败原因、退出码、涉及文件与改动行；截断显式标记。"""
    messages = [
        ToolMessage(tool_call_id="call_1", name="bash", content=_PYTEST_LOG),
        ToolMessage(tool_call_id="call_2", name="git", content=_GIT_DIFF),
        ToolMessage(tool_call_id="call_3", name="search", content=_SEARCH_OUTPUT),
    ]

    transcript = serialize_transcript(messages)

    assert "ZeroDivisionError: division by zero" in transcript
    assert "tests/test_calculator.py:12" in transcript
    assert "exit code 1" in transcript
    assert "+++ b/src/calculator.py" in transcript
    assert 'raise ValueError("division by zero")' in transcript
    assert "tests/test_calculator.py:12:    assert divide(1, 0) == 0" in transcript
    # 每条结果都带工具名与 call id，配对关系不因截断丢失
    assert "[Tool result]: bash (id=call_1)" in transcript
    assert "[Tool result]: git (id=call_2)" in transcript
    # 超长输出按头部截断并标记省略量，不是静默丢弃
    assert "more characters truncated" in transcript
    assert len(_PYTEST_LOG) > TOOL_RESULT_LIMIT


def test_plan_keeps_current_round_and_collects_modified_files_from_old_rounds() -> None:
    """切点只动旧工具轮：工具调用与结果同侧，当前收尾轮原样保留。"""
    path = tool_heavy_path()

    plan = prepare_compaction(path, keep_recent_tokens=50).plan

    assert plan is not None
    # 区域 = system + 两轮工具；保留区 = 当前 user 与收尾
    assert plan.summarized_entry_ids == tuple(entry.id for entry in path[:6])
    assert plan.kept_entry_ids == tuple(entry.id for entry in path[6:])
    assert [type(message).__name__ for message in plan.messages_to_summarize] == [
        "UserMessage",
        "AssistantMessage",
        "ToolMessage",
        "AssistantMessage",
        "ToolMessage",
    ]
    # 被摘要的改动文件进入 CompactionEntry；原文仍留在 JSONL
    assert plan.modified_files == ("src/calculator.py",)
    assert plan.cut.boundary == "user"


def test_summary_input_covers_the_region_and_excludes_the_kept_round() -> None:
    """摘要请求只包含待摘要区域：保留区的输出与 system 快照都不重发。"""
    path = tool_heavy_path()
    kept_output = "kept-round-output-" + "k" * 400
    append_tool_round(path, call_id="call_3", name="read", output=kept_output)
    plan = prepare_compaction(path, keep_recent_tokens=50).plan
    assert plan is not None

    messages = build_summary_messages(serialize_transcript(plan.messages_to_summarize))

    assert isinstance(messages[0], SystemMessage)
    assert messages[0].content == SUMMARIZATION_SYSTEM_PROMPT
    content = messages[1].content
    assert "ZeroDivisionError: division by zero" in content
    assert "+++ b/src/calculator.py" in content
    assert kept_output not in content
    assert "preamble" not in content


def test_cost_model_prices_the_summary_call_before_it_happens() -> None:
    """成本账在调用模型之前算出：截断让摘要请求远小于区域，净收益为正。"""
    plan = prepare_compaction(tool_heavy_path(), keep_recent_tokens=50).plan
    assert plan is not None

    footprint = measure_tool_results(plan.messages_to_summarize)
    cost = estimate_compaction_cost(plan)
    decision = decide_cost_aware_compaction(cost, footprint)

    assert footprint.share > TOOL_RESULT_SHARE_FLOOR
    # 工具原文很大，但序列化后单条只留 2000 字符：摘要请求远小于区域
    assert cost.region_tokens > 4 * cost.summary_input_tokens
    assert cost.summary_output_tokens > 0
    assert cost.saved_per_request == cost.region_tokens - cost.summary_output_tokens
    assert cost.break_even_requests is not None
    assert cost.break_even_requests <= ASSUMED_REMAINING_REQUESTS
    assert cost.net_tokens > 0
    assert decision.should_compact
    assert "break-even" in decision.reason


def test_cost_model_rejects_conversation_heavy_and_tiny_regions() -> None:
    """收益不来自工具结果、或区域小到摘要回不了本时，明确不提前压缩。"""
    path: list[SessionEntry] = []
    append_message(path, SystemMessage(sections={"preamble": "p"}))
    append_message(path, UserMessage(content="a" * _SMALL))
    append_message(path, AssistantMessage(content="b" * 4000))
    append_tool_round(path, call_id="call_1", name="read", output="tiny output")
    append_message(path, UserMessage(content="c" * _SMALL))
    append_message(path, AssistantMessage(content="d" * _SMALL))

    plan = prepare_compaction(path, keep_recent_tokens=50).plan
    assert plan is not None
    footprint = measure_tool_results(plan.messages_to_summarize)
    heavy = decide_cost_aware_compaction(estimate_compaction_cost(plan), footprint)
    assert footprint.share < TOOL_RESULT_SHARE_FLOOR
    assert not heavy.should_compact
    assert "only" in heavy.reason

    tiny_path: list[SessionEntry] = []
    append_message(tiny_path, SystemMessage(sections={"preamble": "p"}))
    append_message(tiny_path, UserMessage(content="a"))
    append_tool_round(tiny_path, call_id="call_1", name="read", output="s" * 400)
    append_message(tiny_path, UserMessage(content="c" * _SMALL))
    append_message(tiny_path, AssistantMessage(content="d" * _SMALL))
    tiny_plan = prepare_compaction(tiny_path, keep_recent_tokens=20).plan
    assert tiny_plan is not None
    tiny_cost = estimate_compaction_cost(tiny_plan)
    tiny = decide_cost_aware_compaction(
        tiny_cost, measure_tool_results(tiny_plan.messages_to_summarize)
    )
    # 区域几乎全是工具结果，但只有约 100 token：摘要体积上界比它大，回不了本
    assert tiny_cost.region_tokens < ASSUMED_SUMMARY_TOKENS
    assert tiny_cost.saved_per_request == 0
    assert tiny_cost.break_even_requests is None
    assert not tiny.should_compact
    assert "not be smaller" in tiny.reason


def test_cost_model_validates_its_assumptions() -> None:
    """成本模型的输入必须成立：请求数假设与区域规模都不能是零。"""
    plan = prepare_compaction(tool_heavy_path(), keep_recent_tokens=50).plan
    assert plan is not None

    with pytest.raises(ValueError, match="assumed_requests"):
        estimate_compaction_cost(plan, assumed_requests=0)
    empty = measure_tool_results([])
    assert empty.share == 0.0
