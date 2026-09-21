from __future__ import annotations


class MiniPiError(Exception):
    """所有项目内显式错误的基类。"""


class ToolError(MiniPiError):
    """Tool 可预期失败：Agent Loop 转为 is_error observation 回传模型。"""


class ToolNotFoundError(ToolError):
    """模型请求了未注册的工具。"""


class ToolArgumentError(ToolError):
    """工具参数缺失、类型错误或未通过校验。"""


class WorkspaceViolationError(ToolError):
    """路径逃逸 workspace 边界。"""


class LLMError(MiniPiError):
    """LLM 层可预期失败，由 LLM Client 编码为 ErrorEvent。"""

    def __init__(self, message: str, *, retryable: bool, status_code: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


class SessionError(MiniPiError):
    """Session 文件或 entry 不符合持久化协议。"""
