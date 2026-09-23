"""M7.5b：摘要请求协议、固定结构与失败语义测试。"""

from __future__ import annotations

import pytest

from mini_pi.context.summarizer import (
    SUMMARIZATION_SYSTEM_PROMPT,
    SummaryResult,
    summarize_transcript,
)
from mini_pi.errors import CompactionError, LLMError
from mini_pi.llm.types import SystemMessage, Usage, UserMessage
from tests.conftest import FakeLLMClient, assistant, tool_call

# 固定的摘要结构；任何一项缺失都会让后续 UPDATE 模板对不上
REQUIRED_SECTIONS = (
    "## Goal",
    "## Constraints & Preferences",
    "## Progress",
    "### Done",
    "### In Progress",
    "### Blocked",
    "## Key Decisions",
    "## Next Steps",
    "## Critical Context",
    "## Files",
    "### Read",
    "### Modified",
)


def test_single_call_without_tools_carries_transcript_and_structure() -> None:
    """一次调用、无工具；历史与固定结构都在同一条 user 消息里。"""
    llm = FakeLLMClient([assistant("## Goal\n修复登陆失败")])
    transcript = "[User]: 修复登录 bug\n\n[Tool result]: read (id=call_1)\nprint('hi')"

    result = summarize_transcript(llm, transcript)

    assert result == SummaryResult(summary="## Goal\n修复登陆失败", usage=None)
    assert llm.tools_seen == [None]
    assert len(llm.calls) == 1
    system, user = llm.calls[0]
    assert isinstance(system, SystemMessage)
    assert system.content == SUMMARIZATION_SYSTEM_PROMPT
    assert isinstance(user, UserMessage)
    assert f"<conversation>\n{transcript}\n</conversation>" in user.content
    for section in REQUIRED_SECTIONS:
        assert section in user.content, section


def test_provider_usage_is_returned_with_summary() -> None:
    """摘要调用的 usage 必须原样带出，供 /compact 展示真实成本。"""
    usage = Usage(input_tokens=1200, output_tokens=300, total_tokens=1500)
    llm = FakeLLMClient([assistant("## Goal\n完成").model_copy(update={"usage": usage})])

    result = summarize_transcript(llm, "[User]: 继续")

    assert result.usage == usage


def test_summary_text_is_stripped() -> None:
    """摘要首尾空白在写盘前去掉，避免 CompactionEntry 校验只看到空白。"""
    llm = FakeLLMClient([assistant("  ## Goal\n完成\n\n")])

    assert summarize_transcript(llm, "[User]: 继续").summary == "## Goal\n完成"


def test_empty_summary_fails() -> None:
    """空摘要不是有效结果，必须失败而不是落盘。"""
    llm = FakeLLMClient([assistant("   \n")])

    with pytest.raises(CompactionError, match="empty"):
        summarize_transcript(llm, "[User]: 继续")


def test_truncated_summary_fails() -> None:
    """length 截断的摘要不完整，不能当作有效检查点。"""
    llm = FakeLLMClient([assistant("## Goal\n写到一半", stop_reason="length")])

    with pytest.raises(CompactionError, match="truncated"):
        summarize_transcript(llm, "[User]: 继续")


def test_error_stop_reason_fails() -> None:
    """assistant 携带 error 状态时同样失败，并保留原始错误信息。"""
    llm = FakeLLMClient(
        [assistant(stop_reason="error").model_copy(update={"error_message": "429 rate limited"})]
    )

    with pytest.raises(CompactionError, match="429 rate limited"):
        summarize_transcript(llm, "[User]: 继续")


def test_unexpected_tool_call_fails() -> None:
    """摘要请求没有提供工具，模型仍返回调用时必须失败。"""
    llm = FakeLLMClient([assistant(tool_calls=[tool_call("call_1", "read", {"path": "a.py"})])])

    with pytest.raises(CompactionError, match="must not call tools: read"):
        summarize_transcript(llm, "[User]: 继续")


def test_llm_error_propagates() -> None:
    """Provider 错误由 LLM 层抛 LLMError，调用器不吞掉也不重试。"""
    llm = FakeLLMClient([LLMError("connection reset", retryable=True)])

    with pytest.raises(LLMError, match="connection reset"):
        summarize_transcript(llm, "[User]: 继续")


def test_empty_transcript_is_rejected_before_calling_model() -> None:
    """空历史属于调用方缺陷，不应产生一次真实模型请求。"""
    llm = FakeLLMClient([])

    with pytest.raises(ValueError, match="transcript must not be empty"):
        summarize_transcript(llm, "   \n")

    assert llm.calls == []
