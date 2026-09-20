from __future__ import annotations

from pydantic import BaseModel, Field

from mini_pi.tools.base import Tool, ToolResult


class EchoArgs(BaseModel):
    text: str = Field(description="Text to echo back.")


class EchoTool(Tool):
    """测试用最小工具：原样返回输入并附带长度。"""

    name = "echo"
    description = "Echo the provided text."
    args_model = EchoArgs

    def execute(self, text: str) -> ToolResult:
        return ToolResult(content=text, details={"length": len(text)})


def test_schema_is_built_from_args_model() -> None:
    """工具 schema 直接来自 pydantic 参数模型，描述与必填字段透传。"""
    schema = EchoTool().schema()
    assert schema.name == "echo"
    assert schema.description == "Echo the provided text."
    assert schema.parameters["properties"]["text"]["description"] == "Text to echo back."
    assert schema.parameters["required"] == ["text"]


def test_execute_returns_structured_result() -> None:
    """execute 返回结构化结果：content 给模型，details 给调用方。"""
    result = EchoTool().execute(text="hi")
    assert result.content == "hi"
    assert result.details == {"length": 2}


def test_tool_result_details_default_none() -> None:
    """details 可省略，表示无额外信息。"""
    assert ToolResult(content="x").details is None
