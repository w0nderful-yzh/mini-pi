"""Tool 抽象与统一返回结构。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, Field, ValidationError

from mini_pi.errors import ToolArgumentError
from mini_pi.llm.types import ToolSchema
from mini_pi.security import ToolAccess


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
    exclusive_batch: ClassVar[bool] = False
    access: ClassVar[ToolAccess] = "approval"

    @property
    def execution_cwd(self) -> Path | None:
        """外部工具默认使用宿主目录；固定 cwd 的 shell 显式覆盖。"""
        return None

    def schema(self) -> ToolSchema:
        """由 pydantic 参数模型生成 JSON Schema，交给模型做参数约束。"""
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters=self.args_model.model_json_schema(),
        )

    def validate_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """内置工具保留 Pydantic 校验和嵌套模型，外部工具可使用原始 JSON Schema。"""
        try:
            validated = self.args_model.model_validate(arguments).model_copy(deep=True)
        except ValidationError as exc:
            raise ToolArgumentError(f"invalid arguments for {self.name!r}: {exc}") from exc
        return dict(validated)

    @property
    def identity(self) -> str:
        """默认审批身份来自宿主类，外部工具显式绑定 server 与原名。"""
        return f"{type(self).__module__}.{type(self).__qualname__}"

    @abstractmethod
    def execute(self, **kwargs: Any) -> ToolResult:
        """执行工具；可预期错误抛 ToolError，非预期异常直接冒泡。"""
