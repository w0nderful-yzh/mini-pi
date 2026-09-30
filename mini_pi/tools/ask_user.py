"""关键问询工具：等待宿主交互后返回真实回答，终端读取由 CLI 完成。"""

from __future__ import annotations

import json
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from mini_pi.errors import InteractionUnavailableError
from mini_pi.interaction import InteractionHandler, InteractionRequest
from mini_pi.tools.base import Tool, ToolResult

NonBlankText = Annotated[str, StringConstraints(strict=True, min_length=1)]


class AskUserArgs(BaseModel):
    """问询参数：单个非空问题和可选的两到三个不同选项。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    question: NonBlankText = Field(max_length=2000)
    options: list[Annotated[NonBlankText, Field(max_length=500)]] | None = Field(
        default=None, min_length=2, max_length=3
    )

    @field_validator("question")
    @classmethod
    def validate_question(cls, value: str) -> str:
        """纯空白问题直接拒绝，保留有效问题的原文。"""
        if not value.strip():
            raise ValueError("question must not be blank")
        return value

    @field_validator("options")
    @classmethod
    def validate_options(cls, value: list[str] | None) -> list[str] | None:
        """拒绝空白和重复选项，不自动改写模型提供的选项。"""
        if value is not None:
            normalized = [option.strip() for option in value]
            if any(not option for option in normalized) or len(set(normalized)) != len(value):
                raise ValueError("options must be non-blank and distinct")
        return value


class AskUserTool(Tool):
    """通过宿主函数同步询问用户，独占工具批次以避免未决策就发生副作用。"""

    name = "ask_user"
    description = (
        "Ask one necessary human decision and wait for the answer. "
        "Optional options must contain 2–3 distinct choices; free text is also supported. "
        "Call this tool alone in its batch."
    )
    args_model = AskUserArgs
    exclusive_batch = True

    def __init__(self, interaction: InteractionHandler | None = None) -> None:
        """保存注入的宿主交互函数；缺少它时不读取 stdin 或猜答案。"""
        self._interaction = interaction

    def execute(self, question: str, options: list[str] | None = None) -> ToolResult:
        """等待回答或取消，交互不可用时交给 Loop 终止本次 run。"""
        if self._interaction is None:
            raise InteractionUnavailableError(
                "ask_user requires interactive input; resume in an interactive terminal "
                "and provide your decision, or inject an interaction handler."
            )
        try:
            response = self._interaction(InteractionRequest(question, tuple(options or ())))
        except EOFError:
            # 关闭输入等同用户取消，不能返回空答案让模型替用户决策。
            raise KeyboardInterrupt from None
        if response.answer is None:
            raise KeyboardInterrupt
        return ToolResult(content=json.dumps(
            {"source": "user", "answer": response.answer}, ensure_ascii=False
        ))
