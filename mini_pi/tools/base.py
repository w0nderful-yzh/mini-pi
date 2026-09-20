"""Tool 抽象与统一返回结构。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from mini_pi.llm.types import ToolSchema


class ToolResult(BaseModel):
    """工具执行结果：content 回传模型，details 仅供 UI / 日志。"""

    content: str
    details: dict[str, Any] | None = None
    # 显式声明本工具改动的 workspace 相对路径；只读工具必须留空
    modified_files: list[str] = Field(default_factory=list)


class Tool(ABC):
    """所有工具的基类，子类声明 name / description / args_model。"""

    name: ClassVar[str]
    description: ClassVar[str]
    args_model: ClassVar[type[BaseModel]]

    def schema(self) -> ToolSchema:
        """由 pydantic 参数模型生成 JSON Schema，交给模型做参数约束。"""
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters=self.args_model.model_json_schema(),
        )

    @abstractmethod
    def execute(self, **kwargs: Any) -> ToolResult:
        """执行工具；可预期错误抛 ToolError，非预期异常直接冒泡。"""
