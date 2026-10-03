"""宿主交互契约：问题和真实回答通过注入函数交换，不依赖终端或消息存储。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class InteractionRequest:
    """一次关键问询；选项仅供用户选择，没有隐含默认答案。"""

    question: str
    options: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class InteractionResponse:
    """一次交互结果；answer 为 None 明确表示用户取消任务。"""

    answer: str | None

    def __post_init__(self) -> None:
        """回答必须是真实非空文本，非法宿主响应直接暴露为程序错误。"""
        if self.answer is not None and (
            not isinstance(self.answer, str) or not self.answer.strip()
        ):
            raise ValueError("interaction answer must be non-blank text or None for cancellation")


InteractionHandler = Callable[[InteractionRequest], InteractionResponse]


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """完整的一次性执行请求；参数已序列化，不向交互端暴露可变工具对象。"""

    target: str
    identity: str
    parameters: str
    cwd: str
    fingerprint: str


@dataclass(frozen=True, slots=True)
class ApprovalResponse:
    """宿主单次审批结果，不接受模型文本或隐含默认值。"""

    decision: Literal["allow", "deny", "cancel"]

    def __post_init__(self) -> None:
        """非法宿主决定属于程序错误，不能隐式当作允许。"""
        if self.decision not in {"allow", "deny", "cancel"}:
            raise ValueError("invalid approval decision")
