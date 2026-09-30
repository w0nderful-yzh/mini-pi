"""问询工具参数和宿主回答契约的离线验证。"""

from __future__ import annotations

import json

import pytest

from mini_pi.errors import InteractionUnavailableError, ToolArgumentError
from mini_pi.interaction import InteractionRequest, InteractionResponse
from mini_pi.tools.ask_user import AskUserTool
from mini_pi.tools.registry import ToolRegistry


@pytest.mark.parametrize("arguments", [
    {"question": " "}, {"question": 42}, {"question": "x" * 2001},
    {"question": "choose", "options": []}, {"question": "choose", "options": ["one"]},
    {"question": "choose", "options": ["a", "b", "c", "d"]},
    {"question": "choose", "options": ["a", " a "]},
    {"question": "choose", "options": ["a", " "]},
    {"question": "choose", "options": ["a", 2]},
    {"question": "choose", "default": "a"},
])
def test_invalid_question_never_invokes_host(arguments: dict[str, object]) -> None:
    """无效问题和默认答案字段显式拒绝，宿主没有机会读取输入。"""
    def forbidden(request: InteractionRequest) -> InteractionResponse:
        """参数验证失败时不应到达宿主。"""
        pytest.fail("host was invoked")

    registry = ToolRegistry()
    registry.register(AskUserTool(forbidden))
    with pytest.raises(ToolArgumentError):
        registry.execute("ask_user", arguments)


def test_answer_contains_real_text_and_no_ui_metadata() -> None:
    """工具只提交用户真实回答，不提交终端编号或选项格式。"""
    questions: list[InteractionRequest] = []

    def answer(request: InteractionRequest) -> InteractionResponse:
        """记录实际问题并返回保留空格和换行的自由文本。"""
        questions.append(request)
        return InteractionResponse("  选择兼容方案\n理由保持原样  ")

    result = AskUserTool(answer).execute("如何迁移？", ["兼容", "直接替换"])

    assert questions == [InteractionRequest("如何迁移？", ("兼容", "直接替换"))]
    assert json.loads(result.content) == {"source": "user", "answer": "  选择兼容方案\n理由保持原样  "}
    assert result.modified_files == [] and result.details is None


def test_missing_host_is_fatal_interaction_error() -> None:
    """无宿主时返回需要结束 run 的错误，不尝试读取 stdin。"""
    with pytest.raises(InteractionUnavailableError, match="interactive"):
        AskUserTool().execute("choose")


@pytest.mark.parametrize("answer", ["", "  ", 1, False])
def test_invalid_host_answer_is_program_defect(answer: object) -> None:
    """宿主不能用空白或隐式转换的值冒充真实回答。"""
    with pytest.raises(ValueError, match="non-blank"):
        InteractionResponse(answer)  # type: ignore[arg-type]
