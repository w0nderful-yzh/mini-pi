"""DeepSeek 客户端：仅覆盖与 OpenAI 的差异。"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from mini_pi.llm.openai_client import OpenAIClient, to_openai_messages
from mini_pi.llm.types import Message


class DeepSeekClient(OpenAIClient):
    """差异点：base_url、API Key 环境变量、assistant 消息回放 reasoning_content。"""

    api_key_env = "DEEPSEEK_API_KEY"

    def __init__(
        self,
        *,
        model: str = "deepseek-flash",
        api_key: str | None = None,
        base_url: str = "https://api.deepseek.com",
        max_retries: int = 2,
        sleep: Callable[[float], None] = time.sleep,
        client: Any = None,
    ) -> None:
        super().__init__(
            model=model,
            api_key=api_key,
            base_url=base_url,
            max_retries=max_retries,
            sleep=sleep,
            client=client,
        )

    def _messages_to_wire(self, messages: list[Message]) -> list[dict[str, Any]]:
        # 开启 include_reasoning，否则多轮对话中 DeepSeek 会拒绝请求
        return to_openai_messages(messages, include_reasoning=True)
