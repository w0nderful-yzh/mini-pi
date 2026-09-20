from __future__ import annotations

import pytest
from pydantic import BaseModel, Field

from mini_pi.errors import ToolArgumentError, ToolError, ToolNotFoundError
from mini_pi.tools.base import Tool, ToolResult
from mini_pi.tools.registry import ToolRegistry


class AddArgs(BaseModel):
    a: int
    b: int = Field(default=1, description="Second operand.")


class AddTool(Tool):
    """测试用加法工具，带默认参数以验证校验与填充。"""

    name = "add"
    description = "Add two integers."
    args_model = AddArgs

    def execute(self, a: int, b: int = 1) -> ToolResult:
        return ToolResult(content=str(a + b))


class ExplodingTool(Tool):
    """始终抛 ToolError，验证错误向上透传而不被 Registry 吞掉。"""

    name = "explode"
    description = "Always fails with ToolError."
    args_model = AddArgs

    def execute(self, a: int, b: int = 1) -> ToolResult:
        raise ToolError("expected failure")


def test_register_and_schemas() -> None:
    """注册后能导出全部工具 schema。"""
    registry = ToolRegistry()
    registry.register(AddTool())
    schemas = registry.schemas()
    assert [schema.name for schema in schemas] == ["add"]


def test_duplicate_registration_is_rejected() -> None:
    """重名注册属于编程错误，立即失败。"""
    registry = ToolRegistry()
    registry.register(AddTool())
    with pytest.raises(ValueError, match="duplicate"):
        registry.register(AddTool())


def test_execute_validates_and_applies_defaults() -> None:
    """execute 统一校验参数并填充默认值后调用工具。"""
    registry = ToolRegistry()
    registry.register(AddTool())
    assert registry.execute("add", {"a": 2}).content == "3"
    assert registry.execute("add", {"a": 2, "b": 5}).content == "7"


def test_unknown_tool_raises() -> None:
    """模型调用未注册工具时报 ToolNotFoundError。"""
    registry = ToolRegistry()
    with pytest.raises(ToolNotFoundError, match="unknown tool"):
        registry.execute("nope", {})


def test_invalid_arguments_raise() -> None:
    """类型错误与缺参都归为 ToolArgumentError，便于 Loop 回传模型。"""
    registry = ToolRegistry()
    registry.register(AddTool())
    with pytest.raises(ToolArgumentError, match="invalid arguments"):
        registry.execute("add", {"a": "not-an-int"})
    with pytest.raises(ToolArgumentError, match="invalid arguments"):
        registry.execute("add", {})


def test_tool_error_propagates() -> None:
    """工具内部抛出的 ToolError 原样向上传递。"""
    registry = ToolRegistry()
    registry.register(ExplodingTool())
    with pytest.raises(ToolError, match="expected failure"):
        registry.execute("explode", {"a": 1})
