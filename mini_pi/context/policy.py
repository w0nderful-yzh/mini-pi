"""上下文窗口策略：显式内置表 + 用户配置，纯函数给出压缩决策。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from mini_pi.context.tokens import TokenEstimate, TokenSource

# 默认为输出留出的 reserve；可在创建 ContextPolicy 时覆盖
DEFAULT_RESERVE_TOKENS = 8_192

# 已知模型的显式上下文窗口；来源为 2026-09 官方文档，未知模型不猜值，
# 必须由用户传入 context_window（如 deepseek-flash / gpt-5.6-* 均为 1M 级）。
KNOWN_CONTEXT_WINDOWS: dict[str, int] = {
    "deepseek-flash": 1_000_000,
    "deepseek-v4-pro": 1_000_000,
    "gpt-6-astra": 1_050_000,
    "gpt-5.6-sol": 1_050_000,
    "gpt-5.6": 1_050_000,  # gpt-5.6-sol 的官方别名
    "gpt-5.6-terra": 1_050_000,
    "gpt-5.6-luna": 1_050_000,
}

CompactionStatus = Literal["not_needed", "needed", "unknown"]


@dataclass(frozen=True, slots=True)
class ContextPolicy:
    """一次决策所需的窗口与预留配置。"""

    context_window: int
    reserve_tokens: int = DEFAULT_RESERVE_TOKENS

    def __post_init__(self) -> None:
        """窗口必须为正，reserve 必须非负且小于窗口，否则阈值无意义。"""
        if self.context_window <= 0:
            raise ValueError("context_window must be > 0")
        if self.reserve_tokens < 0:
            raise ValueError("reserve_tokens must be >= 0")
        if self.reserve_tokens >= self.context_window:
            raise ValueError("reserve_tokens must be smaller than context_window")

    @property
    def threshold_tokens(self) -> int:
        """自动压缩阈值：估算超过它才需要压缩。"""
        return self.context_window - self.reserve_tokens


@dataclass(frozen=True, slots=True)
class CompactionDecision:
    """压缩决策；`source` 暴露估算来源，`reason` 供启动信息展示。"""

    status: CompactionStatus
    reason: str
    source: TokenSource


def resolve_policy(
    model: str,
    *,
    context_window: int | None = None,
    reserve_tokens: int = DEFAULT_RESERVE_TOKENS,
) -> ContextPolicy | None:
    """解析模型策略：用户显式窗口优先，其次内置表；未知模型返回 None。"""
    window = context_window if context_window is not None else KNOWN_CONTEXT_WINDOWS.get(model)
    if window is None:
        return None
    return ContextPolicy(context_window=window, reserve_tokens=reserve_tokens)


def evaluate_compaction(
    estimate: TokenEstimate, *, policy: ContextPolicy | None
) -> CompactionDecision:
    """纯函数决策：无需压缩 / 应压缩 / 无法判断，不修改任何状态。"""
    if policy is None:
        return CompactionDecision(
            status="unknown",
            reason="context window is not configured; automatic compaction is disabled",
            source=estimate.source,
        )
    if estimate.tokens > policy.threshold_tokens:
        return CompactionDecision(
            status="needed",
            reason=(
                f"estimated {estimate.tokens} tokens exceeds threshold "
                f"{policy.threshold_tokens} ({estimate.source})"
            ),
            source=estimate.source,
        )
    return CompactionDecision(
        status="not_needed",
        reason=(
            f"estimated {estimate.tokens} tokens within threshold "
            f"{policy.threshold_tokens} ({estimate.source})"
        ),
        source=estimate.source,
    )
