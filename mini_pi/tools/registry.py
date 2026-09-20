"""Tool 注册、schema 校验与调度。"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from mini_pi.errors import ToolArgumentError, ToolNotFoundError
from mini_pi.llm.types import ToolSchema
from mini_pi.tools.base import Tool, ToolResult


class ToolRegistry:
    """维护 name -> Tool 映射，统一校验参数后再调用工具。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """重名注册属于编程错误，直接失败。"""
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool name: {tool.name}")
        self._tools[tool.name] = tool

    def schemas(self) -> list[ToolSchema]:
        """导出工具 schema，供 LLM 请求使用。"""
        return [tool.schema() for tool in self._tools.values()]

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolNotFoundError(f"unknown tool: {name!r}")
        try:
            validated = tool.args_model.model_validate(arguments)
        except ValidationError as exc:
            # 参数校验失败是可预期失败：转 ToolArgumentError，由 Loop 回传模型纠正
            raise ToolArgumentError(f"invalid arguments for {name!r}: {exc}") from exc
        return tool.execute(**validated.model_dump())
