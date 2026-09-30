"""宿主交互契约：问题和真实回答通过注入函数交换，不依赖终端或消息存储。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


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
