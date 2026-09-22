"""M7.4g：context window、reserve 与阈值策略测试。"""

from __future__ import annotations

import pytest

from mini_pi.context.policy import (
    DEFAULT_RESERVE_TOKENS,
    KNOWN_CONTEXT_WINDOWS,
    ContextPolicy,
    evaluate_compaction,
    resolve_policy,
)
from mini_pi.context.tokens import TokenEstimate


def test_known_model_uses_builtin_window() -> None:
    """内置表给出显式 context window 与默认 reserve。"""
    policy = resolve_policy("deepseek-flash")

    assert policy == ContextPolicy(context_window=1_000_000, reserve_tokens=DEFAULT_RESERVE_TOKENS)
    assert policy is not None
    assert policy.threshold_tokens == 1_000_000 - DEFAULT_RESERVE_TOKENS


def test_known_windows_are_explicit_constants() -> None:
    """内置窗口是明确常量，不做静默猜测。"""
    assert KNOWN_CONTEXT_WINDOWS["deepseek-v4-pro"] == 1_000_000
    assert KNOWN_CONTEXT_WINDOWS["gpt-5.6-terra"] == 1_050_000


def test_unknown_model_without_override_is_disabled() -> None:
    """未知模型未配置窗口时关闭自动压缩。"""
    assert resolve_policy("my-custom-model") is None


def test_unknown_model_accepts_user_context_window() -> None:
    """用户显式提供窗口后即可启用。"""
    policy = resolve_policy("my-custom-model", context_window=32_000, reserve_tokens=2_000)

    assert policy == ContextPolicy(context_window=32_000, reserve_tokens=2_000)


@pytest.mark.parametrize(
    ("context_window", "reserve_tokens"),
    [(0, 10), (-1, 10), (100, -1), (100, 100), (100, 200)],
)
def test_invalid_policy_configuration_is_rejected(
    context_window: int, reserve_tokens: int
) -> None:
    """非法窗口或 reserve 直接报错，不进入决策。"""
    with pytest.raises(ValueError):
        ContextPolicy(context_window=context_window, reserve_tokens=reserve_tokens)


def test_not_needed_when_estimate_equals_threshold() -> None:
    """刚好等于阈值时仍视为无需压缩（严格大于才触发）。"""
    policy = ContextPolicy(context_window=100, reserve_tokens=10)

    decision = evaluate_compaction(TokenEstimate(tokens=90, source="usage"), policy=policy)

    assert decision.status == "not_needed"
    assert decision.source == "usage"
    assert "threshold" in decision.reason


def test_needed_when_estimate_exceeds_threshold() -> None:
    """超过阈值一个 token 就触发。"""
    policy = ContextPolicy(context_window=100, reserve_tokens=10)

    decision = evaluate_compaction(TokenEstimate(tokens=91, source="estimated"), policy=policy)

    assert decision.status == "needed"
    assert decision.source == "estimated"


def test_unknown_when_policy_is_missing() -> None:
    """没有窗口配置时返回 unknown 并说明自动压缩已关闭。"""
    decision = evaluate_compaction(TokenEstimate(tokens=10, source="estimated"), policy=None)

    assert decision.status == "unknown"
    assert "not configured" in decision.reason
    assert "disabled" in decision.reason


def test_decision_carries_estimate_source() -> None:
    """决策必须暴露估算来源，避免把估算当成 provider 精确值。"""
    policy = ContextPolicy(context_window=1000, reserve_tokens=100)

    from_usage = evaluate_compaction(TokenEstimate(tokens=1, source="usage"), policy=policy)
    from_estimate = evaluate_compaction(
        TokenEstimate(tokens=1, source="estimated"), policy=policy
    )

    assert from_usage.source == "usage"
    assert from_estimate.source == "estimated"
    assert from_usage.status == from_estimate.status == "not_needed"


def test_decision_does_not_mutate_inputs() -> None:
    """决策是纯函数：重复调用结果一致。"""
    policy = ContextPolicy(context_window=100, reserve_tokens=10)
    estimate = TokenEstimate(tokens=95, source="estimated")

    first = evaluate_compaction(estimate, policy=policy)
    second = evaluate_compaction(estimate, policy=policy)

    assert first == second
    assert estimate == TokenEstimate(tokens=95, source="estimated")
