from __future__ import annotations

import os

import pytest

from mini_pi.llm.deepseek_client import DeepSeekClient
from mini_pi.llm.openai_client import OpenAIClient
from mini_pi.llm.types import UserMessage

# 真实 API 测试默认被 addopts 排除，仅在显式 -m integration 时运行
pytestmark = pytest.mark.integration


@pytest.mark.skipif(not os.environ.get("OPENAI_API_KEY"), reason="OPENAI_API_KEY not set")
def test_openai_complete() -> None:
    """真实调用 OpenAI，验证流式聚合与基础对话可用。"""
    client = OpenAIClient(model=os.environ.get("MINI_PI_OPENAI_MODEL", "gpt-4o-mini"))
    message = client.complete([UserMessage(content="Reply with exactly: ok")])
    assert message.content.strip().lower().startswith("ok")


@pytest.mark.skipif(not os.environ.get("DEEPSEEK_API_KEY"), reason="DEEPSEEK_API_KEY not set")
def test_deepseek_complete() -> None:
    """真实调用 DeepSeek，验证 base_url 与 reasoning 回放不影响普通对话。"""
    client = DeepSeekClient(model=os.environ.get("MINI_PI_DEEPSEEK_MODEL", "deepseek-chat"))
    message = client.complete([UserMessage(content="Reply with exactly: ok")])
    assert message.content.strip().lower().startswith("ok")
