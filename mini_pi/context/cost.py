"""M7.6f：旧工具结果提前压缩的 token 成本模型（纯函数，不调用模型、不写盘）。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from mini_pi.context.compaction import CompactionPlan
from mini_pi.context.serializer import serialize_transcript
from mini_pi.context.summarizer import build_summary_messages
from mini_pi.context.tokens import estimate_text_tokens, estimate_tokens
from mini_pi.llm.types import Message, ToolMessage

# 摘要体积的保守上界：模板要求 “Keep each section concise”，实测摘要远小于此。首次
# 压缩没有历史摘要可参考时用它估上界——宁可少压缩，也不虚报收益。
ASSUMED_SUMMARY_TOKENS = 2_000

# 提前压缩要求覆盖的后续请求数：摘要调用自己必须重发一次被摘要区域，因此少于 2 次
# 后续请求必然亏损；取 3 留余量。基准样本 H1 单任务 11 次请求，工具轮之后通常还有
# 多次请求（docs/benchmarks/m7-c6-usage-baseline.md）。
ASSUMED_REMAINING_REQUESTS = 3

# 旧工具结果在被摘要区域中的 token 占比下限：低于它说明收益主要来自普通对话，摘要掉
# 模型自己的推理与用户原话代价更高，这类历史交给窗口阈值处理。
TOOL_RESULT_SHARE_FLOOR = 0.5


@dataclass(frozen=True, slots=True)
class ToolResultFootprint:
    """被摘要区域里的旧工具结果规模；用于判断压缩收益是否来自 observation。"""

    tokens: int
    messages: int
    region_tokens: int

    @property
    def share(self) -> float:
        """工具结果占区域 token 的比例；区域为空时为 0。"""
        if self.region_tokens <= 0:
            return 0.0
        return self.tokens / self.region_tokens


@dataclass(frozen=True, slots=True)
class CompactionCost:
    """一次提前压缩的 token 账：去掉多少、摘要花多少、几次请求回本。"""

    region_tokens: int
    summary_input_tokens: int
    summary_output_tokens: int
    assumed_requests: int
    saved_per_request: int
    # None 表示摘要不比区域小，永远回不了本
    break_even_requests: int | None
    # 按 assumed_requests 计算的净收益；可负，负数表示不值得压缩
    net_tokens: int


@dataclass(frozen=True, slots=True)
class CostDecision:
    """成本触发的判定结果；`reason` 同时用于日志与测试断言。"""

    should_compact: bool
    reason: str


def measure_tool_results(messages: Sequence[Message]) -> ToolResultFootprint:
    """统计待摘要区域里 ToolMessage 的 token 与条数；只读，不调用模型。"""
    items = list(messages)
    tool_messages = [message for message in items if isinstance(message, ToolMessage)]
    return ToolResultFootprint(
        tokens=sum(estimate_tokens([message]).tokens for message in tool_messages),
        messages=len(tool_messages),
        region_tokens=estimate_tokens(items).tokens,
    )


def estimate_compaction_cost(
    plan: CompactionPlan, *, assumed_requests: int = ASSUMED_REMAINING_REQUESTS
) -> CompactionCost:
    """在调用摘要模型之前算出这次压缩的 token 账；纯函数，无副作用。

    - 摘要请求的输入按真实构造方式估算（含模板与历史摘要），不是按区域大小猜
    - 摘要体积优先用上一次摘要的实测规模；首次压缩用保守上界
    - `saved_per_request` 是之后每次请求少发的 token，摘要自身也要占用投影
    """
    if assumed_requests <= 0:
        raise ValueError("assumed_requests must be > 0")
    region_tokens = estimate_tokens(plan.messages_to_summarize).tokens
    transcript = serialize_transcript(plan.messages_to_summarize)
    summary_input_tokens = estimate_tokens(
        build_summary_messages(transcript, previous_summary=plan.previous_summary)
    ).tokens
    if plan.previous_summary is None:
        summary_output_tokens = ASSUMED_SUMMARY_TOKENS
    else:
        summary_output_tokens = estimate_text_tokens(plan.previous_summary)
    saved_per_request = max(0, region_tokens - summary_output_tokens)
    if saved_per_request <= 0:
        break_even_requests = None
    else:
        # 摘要调用一次性花掉 input+output，之后每次请求省 saved_per_request
        cost = summary_input_tokens + summary_output_tokens
        break_even_requests = -(-cost // saved_per_request)
    return CompactionCost(
        region_tokens=region_tokens,
        summary_input_tokens=summary_input_tokens,
        summary_output_tokens=summary_output_tokens,
        assumed_requests=assumed_requests,
        saved_per_request=saved_per_request,
        break_even_requests=break_even_requests,
        net_tokens=saved_per_request * assumed_requests
        - (summary_input_tokens + summary_output_tokens),
    )


def decide_cost_aware_compaction(
    cost: CompactionCost, footprint: ToolResultFootprint
) -> CostDecision:
    """判断是否值得提前压缩：收益必须来自旧工具结果，且覆盖摘要调用成本。"""
    if footprint.share < TOOL_RESULT_SHARE_FLOOR:
        return CostDecision(
            should_compact=False,
            reason=(
                f"tool results are only {footprint.share:.0%} of the "
                f"{footprint.region_tokens} tokens to summarize"
            ),
        )
    if cost.saved_per_request <= 0:
        return CostDecision(
            should_compact=False,
            reason="the summary would not be smaller than the region it replaces",
        )
    if cost.net_tokens <= 0:
        return CostDecision(
            should_compact=False,
            reason=(
                f"summary call costs {cost.summary_input_tokens} in + "
                f"{cost.summary_output_tokens} out tokens, which "
                f"{cost.assumed_requests} more requests do not pay back"
            ),
        )
    return CostDecision(
        should_compact=True,
        reason=(
            f"saves {cost.saved_per_request} tokens per request over "
            f"{cost.assumed_requests} assumed requests after a "
            f"{cost.summary_input_tokens}+{cost.summary_output_tokens} token summary call "
            f"(net {cost.net_tokens}, break-even {cost.break_even_requests} requests)"
        ),
    )
