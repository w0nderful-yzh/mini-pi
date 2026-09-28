"""用固定的中英文与工具请求对拍 DeepSeek 输入 usage 和本地预测。"""

from __future__ import annotations

import json

from mini_pi.auth import resolve_api_key
from mini_pi.context.request import estimate_request
from mini_pi.llm.deepseek_client import DeepSeekClient
from mini_pi.llm.types import SystemMessage, ToolSchema, UserMessage

MODEL = "deepseek-flash"
SYSTEM = SystemMessage(content="请只回复：收到。")
CHINESE = "请检查这个函数的输入边界，说明空值、重复值和路径越界时应该怎样处理。"
ENGLISH = "Review the function input boundaries and explain how empty, duplicate, and escaped paths should be handled. "
SCHEMA = ToolSchema(
    name="inspect_file",
    description="读取工作区内的文本文件并返回内容。Read a text file within the workspace.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "工作区相对路径。Relative workspace path."},
            "offset": {"type": "integer", "description": "起始行号。Starting line number."},
            "limit": {"type": "integer", "description": "最多读取行数。Maximum lines."},
        },
        "required": ["path"],
        "additionalProperties": False,
    },
)


def main() -> None:
    """发送四组固定请求，仅输出尺寸与 token 数，不记录凭据或模型回复。"""
    key = resolve_api_key("deepseek", env_var="DEEPSEEK_API_KEY")
    if not key:
        raise RuntimeError("DeepSeek API Key 不可用")
    client = DeepSeekClient(model=MODEL, api_key=key)
    cases = (
        ("chinese", CHINESE * 12, []),
        ("english", ENGLISH * 8, []),
        ("mixed", (CHINESE + ENGLISH) * 6, []),
        ("schema", "请回复收到。", [SCHEMA]),
    )
    for name, content, tools in cases:
        messages = [SYSTEM, UserMessage(content=content)]
        prediction = estimate_request(messages, tools, provider="deepseek", model=MODEL)
        response = client.complete(messages, tools)
        if response.usage is None or response.usage.input_tokens <= 0:
            raise RuntimeError(f"{name}: Provider 未返回有效 input usage")
        actual = response.usage.input_tokens
        print(
            json.dumps(
                {
                    "case": name,
                    "model": MODEL,
                    "user_chars": len(content),
                    "tools": len(tools),
                    "estimate": prediction.input_tokens,
                    "input_usage": actual,
                    "error_pct": round((prediction.input_tokens / actual - 1) * 100, 1),
                },
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    main()
