"""CLI 关键问询：收起活动区后读取真实回答，编号和提示只留在终端。"""

from __future__ import annotations

import sys

from mini_pi.cli.console import ConsoleRenderer
from mini_pi.errors import InteractionUnavailableError
from mini_pi.interaction import InteractionHandler, InteractionRequest, InteractionResponse


def _interactive_terminal(renderer: ConsoleRenderer) -> bool:
    """问询要求输入和输出都在终端上，管道不能隐式消耗后续任务文本。"""
    return bool(sys.stdin.isatty() and renderer.console.is_terminal)


def create_interaction_handler(renderer: ConsoleRenderer) -> InteractionHandler:
    """绑定当前终端渲染器，不提前读取输入，也不把交互状态存入 Session。"""
    def ask(request: InteractionRequest) -> InteractionResponse:
        """等一次真实提交，空答案或非法序号只在本地重问。"""
        if not _interactive_terminal(renderer):
            raise InteractionUnavailableError(
                "ask_user requires a TTY for input and output; resume in an interactive "
                "terminal and provide your decision."
            )
        renderer.prepare_for_interaction()

        def show(text: str) -> None:
            """先脱敏再清除终端控制字符，以纯文本折行显示外部问题。"""
            clean = "".join(
                char for char in renderer.redact(text) if char.isprintable() or char == "\n"
            )
            renderer.console.print(clean, markup=False, highlight=False)

        show("Decision needed")
        show(request.question)
        for index, option in enumerate(request.options, 1):
            show(f"  {index}. {option}")
        show("Choose a number or enter text. /cancel, Ctrl+C or EOF cancels the task."
             if request.options else "Enter your answer. /cancel, Ctrl+C or EOF cancels the task.")
        while True:
            # 问询不用 REPL 历史或命令补全，回答不会旁路存入本地输入历史。
            answer = input("Answer › ")
            stripped = answer.strip()
            if not stripped:
                show("An answer is required; no default is selected.")
                continue
            if stripped == "/cancel":
                return InteractionResponse(None)
            if request.options and stripped.isdecimal():
                choices = {str(index): option for index, option in enumerate(request.options, 1)}
                if stripped not in choices:
                    show(f"Choose 1–{len(request.options)}, enter text, or /cancel.")
                    continue
                return InteractionResponse(choices[stripped])
            return InteractionResponse(answer)

    return ask
