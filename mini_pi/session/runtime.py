"""AgentSession：装配 Agent 与追加式 JSONL Session 的创建和恢复模式。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from mini_pi.agent.agent import Agent
from mini_pi.agent.events import AgentEvent
from mini_pi.agent.state import AgentState
from mini_pi.auth import resolve_api_key
from mini_pi.context.compaction import (
    CompactionResult,
    generate_compaction_result,
    prepare_compaction,
)
from mini_pi.context.projection import project_compaction, project_entry_path
from mini_pi.errors import MiniPiError, SessionError
from mini_pi.llm.base import LLMClient
from mini_pi.llm.deepseek_client import DeepSeekClient
from mini_pi.llm.openai_client import OpenAIClient
from mini_pi.llm.types import AssistantMessage, Message, ToolSchema
from mini_pi.session.jsonl import JsonlSession
from mini_pi.session.usage import RunUsage, recent_session_run_usage
from mini_pi.tools.registry import ToolRegistry

LLMFactory = Callable[[str, str], LLMClient]


@dataclass(frozen=True, slots=True)
class CompactionExecution:
    """一次手动压缩的执行结果；result 为 None 时 reason 说明为什么没有压缩。"""

    result: CompactionResult | None
    reason: str


def _create_auth_llm(provider: str, model: str) -> LLMClient:
    """仅从环境变量或用户认证文件解析 Key，构造受支持的 Provider。"""
    client_type: type[OpenAIClient]
    if provider == "openai":
        client_type = OpenAIClient
    elif provider == "deepseek":
        client_type = DeepSeekClient
    else:
        raise SessionError(f"unsupported session provider: {provider!r}")
    api_key = resolve_api_key(provider, env_var=client_type.api_key_env)
    if not api_key:
        raise MiniPiError(
            f"{provider} API key is not configured; "
            f"set {client_type.api_key_env} or use /connect"
        )
    return client_type(model=model, api_key=api_key)


class AgentSession:
    """装配 Agent 和 JSONL 会话，创建或恢复后同步提交完整消息。"""

    def __init__(
        self,
        *,
        session: JsonlSession,
        llm: LLMClient,
        registry: ToolRegistry,
        max_steps: int,
        max_run_input_tokens: int | None,
        on_event: Callable[[AgentEvent], None] | None,
        provider: str,
        model: str,
    ) -> None:
        """持有会话/依赖并构建 Agent，将提交回调接到自身的 durable-first 入口。"""
        self._session = session
        self._llm = llm
        self._registry = registry
        self._max_steps = max_steps
        self._max_run_input_tokens = max_run_input_tokens
        self._on_event = on_event
        self._provider = provider
        self._model = model
        self._agent = Agent(
            llm=llm,
            registry=registry,
            cwd=session.header.cwd,
            max_steps=max_steps,
            max_run_input_tokens=max_run_input_tokens,
            on_event=on_event,
            on_message_commit=self._commit_message,
        )

    @classmethod
    def create(
        cls,
        *,
        cwd: str | Path,
        llm: LLMClient,
        registry: ToolRegistry,
        provider: str,
        model: str,
        sessions_root: str | Path | None = None,
        max_steps: int = 50,
        max_run_input_tokens: int | None = None,
        on_event: Callable[[AgentEvent], None] | None = None,
    ) -> AgentSession:
        """校验运行参数后创建 header，并接通 durable-first 消息提交。"""
        if max_steps <= 0:
            raise ValueError("max_steps must be > 0")
        if max_run_input_tokens is not None and max_run_input_tokens <= 0:
            raise ValueError("max_run_input_tokens must be > 0")
        session = JsonlSession.create(
            cwd=cwd,
            provider=provider,
            model=model,
            sessions_root=sessions_root,
        )
        return cls(
            session=session,
            llm=llm,
            registry=registry,
            max_steps=max_steps,
            max_run_input_tokens=max_run_input_tokens,
            on_event=on_event,
            provider=provider,
            model=model,
        )

    @classmethod
    def resume(
        cls,
        path: str | Path,
        *,
        registry: ToolRegistry,
        cwd: str | Path | None = None,
        provider: str | None = None,
        model: str | None = None,
        llm_factory: LLMFactory | None = None,
        max_steps: int = 50,
        max_run_input_tokens: int | None = None,
        on_event: Callable[[AgentEvent], None] | None = None,
    ) -> AgentSession:
        """从活动 leaf 恢复状态和模型配置，后续消息沿原 leaf 追加。"""
        if max_steps <= 0:
            raise ValueError("max_steps must be > 0")
        if max_run_input_tokens is not None and max_run_input_tokens <= 0:
            raise ValueError("max_run_input_tokens must be > 0")
        session = JsonlSession.load(path, expected_cwd=cwd)
        if not session.header.cwd.is_dir():
            raise SessionError(f"session cwd does not exist: {session.header.cwd}")
        replay = session.replay()
        resolved_provider = replay.provider if provider is None else provider
        resolved_model = replay.model if model is None else model
        if not resolved_provider.strip() or not resolved_model.strip():
            raise ValueError("provider and model must not be empty")
        if resolved_provider not in {"openai", "deepseek"}:
            raise SessionError(f"unsupported session provider: {resolved_provider!r}")
        # 真实运行只读认证配置；可注入离线构造函数以验证恢复行为。
        factory = _create_auth_llm if llm_factory is None else llm_factory
        llm = factory(resolved_provider, resolved_model)
        runtime = cls(
            session=session,
            llm=llm,
            registry=registry,
            max_steps=max_steps,
            max_run_input_tokens=max_run_input_tokens,
            on_event=on_event,
            provider=resolved_provider,
            model=resolved_model,
        )
        runtime.state.messages.extend(replay.messages)
        runtime.state.step_count = replay.step_count
        runtime.state.modified_files.update(replay.modified_files)
        return runtime

    @property
    def path(self) -> Path:
        """返回当前会话的 JSONL 路径。"""
        return self._session.path

    @property
    def state(self) -> AgentState:
        """暴露当前 Agent 的内存投影供调用方检查。"""
        return self._agent.state

    @property
    def provider(self) -> str:
        """返回后续消息将记录的 Provider。"""
        return self._provider

    @property
    def model(self) -> str:
        """返回后续消息将记录的模型。"""
        return self._model

    @property
    def session_id(self) -> str:
        """返回 JSONL header 的稳定会话标识。"""
        return str(self._session.header.id)

    @property
    def tool_schemas(self) -> list[ToolSchema]:
        """向 CLI 暴露当前 Registry 的工具说明。"""
        return self._registry.schemas()

    @property
    def summary_index(self) -> int | None:
        """当前投影含有效 compaction 时，摘要固定在 system 快照之后。"""
        path = project_entry_path(self._session.entries, leaf_id=self._session.leaf_id)
        return 1 if project_compaction(path) is not None else None

    @property
    def last_run_usage(self) -> RunUsage | None:
        """只读活动链中的最近任务；resume 后仍可回看原始 usage。"""
        return recent_session_run_usage(self._session.active_entries())

    @property
    def max_run_input_tokens(self) -> int | None:
        """返回每次 run 重置的显式累计输入预算。"""
        return self._max_run_input_tokens

    def new(self, *, sessions_root: str | Path | None = None) -> AgentSession:
        """用当前模型和工具创建独立会话，保留旧 JSONL 以供恢复。"""
        return self.create(
            cwd=self._session.header.cwd,
            llm=self._llm,
            registry=self._registry,
            provider=self._provider,
            model=self._model,
            sessions_root=sessions_root,
            max_steps=self._max_steps,
            max_run_input_tokens=self._max_run_input_tokens,
            on_event=self._on_event,
        )

    def set_llm(self, llm: LLMClient, *, provider: str, model: str) -> None:
        """替换运行时客户端，仅让后续提交消息使用新的模型元数据。"""
        if provider not in {"openai", "deepseek"} or not model.strip():
            raise SessionError("provider or model is invalid for this session")
        self._agent.set_llm(llm)
        self._llm = llm
        self._provider = provider
        self._model = model

    def run(self, task: str) -> AssistantMessage:
        """执行一轮任务，完整消息由提交回调立即持久化。"""
        return self._agent.run(task)

    def compact(self, *, keep_recent_tokens: int) -> CompactionExecution:
        """对当前投影执行一次手动压缩；没有安全切点时不做任何改动。

        - 摘要调用走当前会话的 LLM，失败（LLMError / CompactionError）直接冒泡
        - 只有摘要成功后才写 CompactionEntry，再从 entry 路径重建内存投影
        """
        preparation = prepare_compaction(
            self._session.active_entries(), keep_recent_tokens=keep_recent_tokens
        )
        if preparation.plan is None:
            return CompactionExecution(result=None, reason=preparation.reason)
        result = generate_compaction_result(preparation.plan, self._llm)
        self._commit_compaction(result)
        return CompactionExecution(result=result, reason=preparation.reason)

    def _commit_compaction(self, result: CompactionResult) -> None:
        """摘要成功后追加 CompactionEntry，再整体替换内存投影。

        先落盘、后重建：写盘失败时 JSONL 与内存都不变；重建失败时保留旧投影，
        AgentState 不会被写成半成品（JSONL 里是完整 entry，新进程 resume 可重建）。
        """
        plan = result.plan
        self._session.append_compaction(
            summary=result.summary,
            first_kept_entry_id=plan.first_kept_entry_id,
            tokens_before=plan.tokens_before.tokens,
            system_message=plan.system_message,
            usage=result.usage,
            modified_files=list(plan.modified_files),
        )
        messages = self._session.replay().messages
        self._agent.state.messages = list(messages)

    def _commit_message(self, message: Message) -> None:
        """先追加 JSONL；失败时让 Agent 的 durable-first 入口停止。"""
        # system/user 位于下一轮开始前，assistant/tool 位于 Loop 步数递增后。
        self._session.append_message(
            message,
            provider=self._provider,
            model=self._model,
            step_count=self._agent.state.step_count,
        )
