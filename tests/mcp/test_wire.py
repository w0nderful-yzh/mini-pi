"""两个 Provider 的真实 SDK 请求保留 MCP 名称和原始约束。"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import openai
import pytest

from mini_pi.llm.deepseek_client import DeepSeekClient
from mini_pi.llm.openai_client import OpenAIClient
from mini_pi.llm.types import AssistantMessage, ToolCall, ToolMessage, UserMessage
from mini_pi.mcp.catalog import tool_alias
from tests.mcp.test_catalog import registry


@pytest.mark.parametrize("provider", [OpenAIClient, DeepSeekClient])
def test_provider_wire_has_stable_alias_and_original_refs(
    provider: type[OpenAIClient], tmp_path: Path
) -> None:
    """MockTransport 捕获 SDK 实际发出的 JSON，不仅测试转换函数。"""
    captured: list[dict] = []

    def respond(request: httpx.Request) -> httpx.Response:
        """离线流式响应包含 usage，使两个客户端走完整 production 请求路径。"""
        captured.append(json.loads(request.content))
        body = 'data: {"id":"test","choices":[{"index":0,"delta":{"content":"done"},"finish_reason":null}]}\n\ndata: {"id":"test","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":123,"completion_tokens":1,"total_tokens":124}}\n\ndata: [DONE]\n\n'
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    active = registry(tmp_path)
    transport = httpx.Client(transport=httpx.MockTransport(respond))
    sdk = openai.OpenAI(
        api_key="fake-key", base_url="https://offline.invalid", http_client=transport
    )
    try:
        active.prepare_mcp()
        alias = tool_alias("local", "calc.add")
        messages = [
            UserMessage(content="calculate"),
            AssistantMessage(
                tool_calls=[ToolCall(id="c1", name=alias, arguments={"value": 2})],
                stop_reason="tool_calls",
            ),
            ToolMessage(tool_call_id="c1", name=alias, content="3"),
        ]
        result = provider(client=sdk, api_key="fake-key", model="offline-test").complete(
            messages, tools=active.schemas()
        )
        assert result.usage.input_tokens == 123
        wire = captured[0]
        definition = next(
            item["function"] for item in wire["tools"] if item["function"]["name"] == alias
        )
        assert (
            definition["parameters"]["required"] == ["value"]
            and definition["parameters"]["properties"]["value"]["$ref"] == "#/$defs/number"
        )
        assert wire["messages"][1]["tool_calls"][0]["function"]["name"] == alias
        assert len(definition["name"]) <= 64
    finally:
        active.close()
        sdk.close()
