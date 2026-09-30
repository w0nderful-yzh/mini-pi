"""M8.3 区域成本估算：历史整次请求 usage 不属于被摘要片段。"""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

from mini_pi.context.compaction import CompactionPlan, CutPoint
from mini_pi.context.cost import estimate_compaction_cost, measure_tool_results
from mini_pi.context.tokens import TokenEstimate
from mini_pi.llm.types import AssistantMessage, SystemMessage, ToolCall, ToolMessage, Usage


def test_region_cost_and_tool_share_ignore_historical_usage() -> None:
    """相同片段有无 usage 应得相同成本与占比，不能吞入片段之外的历史。"""
    assistant = AssistantMessage(tool_calls=[ToolCall(id="c1", name="read", arguments={})])
    result = ToolMessage(tool_call_id="c1", name="read", content="x" * 30_000)
    plan = CompactionPlan(
        cut=CutPoint(start_index=2, boundary="user", kept_tokens=20),
        messages_to_summarize=(assistant, result),
        previous_summary=None,
        summarized_entry_ids=(uuid4(), uuid4()),
        kept_entry_ids=(uuid4(),),
        tokens_before=TokenEstimate(tokens=6_000, source="estimated"),
        system_message=SystemMessage(content="rules"),
        modified_files=(),
    )
    measured = replace(plan, messages_to_summarize=(
        assistant.model_copy(update={
            "usage": Usage(input_tokens=90_000, output_tokens=100, total_tokens=90_100)
        }), result,
    ))

    assert estimate_compaction_cost(measured) == estimate_compaction_cost(plan)
    assert measure_tool_results(measured.messages_to_summarize) == measure_tool_results(
        plan.messages_to_summarize
    )
    assert measure_tool_results(measured.messages_to_summarize).share > 0.99
