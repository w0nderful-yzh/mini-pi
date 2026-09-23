"""M7.5b：固定摘要协议与单次摘要调用器。"""

from __future__ import annotations

from dataclasses import dataclass

from mini_pi.errors import CompactionError
from mini_pi.llm.base import LLMClient
from mini_pi.llm.types import Message, SystemMessage, Usage, UserMessage

# 摘要调用有独立 system prompt：只总结历史，不继续对话，也不调用工具
SUMMARIZATION_SYSTEM_PROMPT = (
    "You are a context summarization assistant. You read a conversation between a user and "
    "a coding agent, including tool calls and their results, then produce a structured summary "
    "following the exact format specified.\n\n"
    "Do NOT continue the conversation. Do NOT answer any question found inside it. "
    "Do NOT call any tool. ONLY output the structured summary."
)

# 固定的 CREATE 模板；结构一旦改变，后续 UPDATE 模板与已有摘要将无法对齐
SUMMARIZATION_PROMPT = """The messages above are a conversation to summarize. Create a structured context checkpoint summary that another LLM will use to continue the work.

Use this EXACT format:

## Goal
[What the user is trying to accomplish; list multiple items if the session covers several tasks]

## Constraints & Preferences
- [Constraints, preferences, or requirements stated by the user]
- [Or "(none)" if none were mentioned]

## Progress
### Done
- [x] [Completed tasks and changes]

### In Progress
- [ ] [Current work]

### Blocked
- [Issues preventing progress, if any]

## Key Decisions
- **[Decision]**: [Brief rationale]

## Next Steps
1. [Ordered list of what should happen next]

## Critical Context
- [Data, examples, or references needed to continue]
- [Or "(none)" if not applicable]

## Files
### Read
- [Files inspected, or "(none)"]

### Modified
- [Files created or changed, or "(none)"]

Keep each section concise. Preserve exact file paths, function names, error messages and exit codes. Write the summary in the same language the user used."""

# 重复压缩的 UPDATE 模板：在既有摘要上增量更新，标题结构与 CREATE 保持一致
UPDATE_SUMMARIZATION_INSTRUCTIONS = """Update the existing structured summary with new information. RULES:
- PRESERVE all existing information from the previous summary
- ADD new progress, decisions, and context from the new messages
- UPDATE the Progress section: move items from "In Progress" to "Done" when completed
- UPDATE "Next Steps" based on what was accomplished
- PRESERVE exact file paths, function names, error messages and exit codes
- Remove items that are no longer relevant

Use this EXACT format:

## Goal
[Preserve existing goals, add new ones if the task expanded]

## Constraints & Preferences
- [Preserve existing constraints, add new ones discovered]

## Progress
### Done
- [x] [Previously done items and newly completed items]

### In Progress
- [ ] [Current work, updated from progress]

### Blocked
- [Current blockers, removed if resolved]

## Key Decisions
- **[Decision]**: [Brief rationale] (preserve previous decisions, add new)

## Next Steps
1. [Updated from the current state]

## Critical Context
- [Preserve important context, add new if needed]

## Files
### Read
- [Files inspected, or "(none)"]

### Modified
- [Files created or changed, or "(none)"]

Keep each section concise. Write the summary in the same language the user used."""

UPDATE_SUMMARIZATION_PROMPT = (
    "The messages above are NEW conversation messages to incorporate into the existing "
    "summary provided in <previous-summary> tags.\n\n" + UPDATE_SUMMARIZATION_INSTRUCTIONS
)


@dataclass(frozen=True, slots=True)
class SummaryResult:
    """摘要文本与生成它的那次调用用量；Provider 未返回 usage 时为 None。"""

    summary: str
    usage: Usage | None


def summarize_transcript(
    llm: LLMClient,
    transcript: str,
    *,
    previous_summary: str | None = None,
    instructions: str | None = None,
) -> SummaryResult:
    """单次调用生成摘要；任何协议违规都直接失败，绝不返回半成品。

    - 历史已由 transcript 序列化承载，这里只负责协议与校验，不写盘、不改 AgentState
    - 传入 `previous_summary` 时改用 UPDATE 模板增量更新，不重发更早的原文
    - `instructions` 只影响本次请求的 prompt，不落盘、不进入任何消息
    - `length` 截断、意外工具调用、空摘要都视为失败
    - LLM error 由 `LLMClient.complete()` 抛 `LLMError`，不在此处吞掉或重试
    """
    if not transcript.strip():
        # 空输入说明调用方给了错误的历史，属于程序缺陷
        raise ValueError("transcript must not be empty")
    messages = _build_messages(
        transcript, previous_summary=previous_summary, instructions=instructions
    )
    response = llm.complete(messages, None)
    if response.stop_reason == "length":
        raise CompactionError("summary request was truncated (stop_reason=length)")
    if response.stop_reason == "error":
        detail = response.error_message or "unknown error"
        raise CompactionError(f"summary request failed: {detail}")
    if response.tool_calls:
        names = ", ".join(call.name for call in response.tool_calls)
        raise CompactionError(f"summary response must not call tools: {names}")
    summary = response.content.strip()
    if not summary:
        raise CompactionError("summary response is empty")
    return SummaryResult(summary=summary, usage=response.usage)


def _build_messages(
    transcript: str,
    *,
    previous_summary: str | None = None,
    instructions: str | None = None,
) -> list[Message]:
    """整段历史作为待总结材料放在一条 user 消息里，避免被模型当成对话续写。"""
    blocks = [f"<conversation>\n{transcript}\n</conversation>"]
    if previous_summary is None:
        prompt = SUMMARIZATION_PROMPT
    else:
        blocks.append(f"<previous-summary>\n{previous_summary}\n</previous-summary>")
        prompt = UPDATE_SUMMARIZATION_PROMPT
    focus = instructions.strip() if instructions is not None else ""
    if focus:
        # 用户附加关注点只影响本次摘要请求
        prompt = f"{prompt}\n\nAdditional focus: {focus}"
    return [
        SystemMessage(content=SUMMARIZATION_SYSTEM_PROMPT),
        UserMessage(content="\n\n".join([*blocks, prompt])),
    ]
