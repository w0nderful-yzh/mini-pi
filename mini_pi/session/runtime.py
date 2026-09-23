"""AgentSession：装配 Agent 与追加式 JSONL Session 的创建和恢复模式。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from mini_pi.agent.agent import Agent
from mini_pi.agent.events import AgentEndEvent, AgentEvent, AgentStartEvent
from mini_pi.agent.state import AgentState
from mini_pi.auth import resolve_api_key
from mini_pi.context.compaction import (
    CompactionPreparation,
    CompactionResult,
    generate_compaction_result,
    prepare_compaction,
)
from mini_pi.context.cost import (
    decide_cost_aware_compaction,
    estimate_compaction_cost,
    measure_tool_results,
)
from mini_pi.context.policy import (
    DEFAULT_KEEP_RECENT_TOKENS,
    evaluate_compaction,
    resolve_policy,
)
from mini_pi.context.projection import project_compaction, project_entry_path
from mini_pi.context.tokens import estimate_tokens
from mini_pi.errors import CompactionError, LLMError, MiniPiError, SessionError
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


def _compaction_failure(reason: str) -> str:
    """自动压缩无法把投影降到阈值内时的终止原因；由调用方转成 agent error。

    文案只陈述做了什么与没做什么，不声称 Session 状态；状态保留由事务与测试保证。
    """
    return (
        f"automatic compaction failed: {reason}. "
        "The run stopped before sending the next model request."
    )


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
        # 成本感知的提前压缩每次 run 只尝试一次：失败也不重复打扰模型
        self._cost_compaction_attempted = False
        self._agent = Agent(
            llm=llm,
            registry=registry,
            cwd=session.header.cwd,
            max_steps=max_steps,
            max_run_input_tokens=max_run_input_tokens,
            on_event=on_event,
            on_message_commit=self._commit_message,
            # 工具轮之间复用同一策略与事务：下一次请求读到压缩后的投影
            prepare_next_turn=self._compact_between_turns,
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
        """执行一轮任务：先按窗口策略判断是否需要压缩，再提交这一条 user 消息。"""
        if not task.strip():
            # 空任务不触发压缩检查，也不产生任何 Session 写入
            raise ValueError("task must not be empty")
        self._cost_compaction_attempted = False
        failure = self._auto_compact_if_needed()
        if failure is not None:
            # 压缩没能把投影降到阈值内：这次任务以明确 agent error 结束
            return self._abort_before_prompt(failure)
        return self._agent.run(task)

    def _auto_compact_if_needed(self) -> str | None:
        """按 M7.4g 窗口策略检查当前投影；返回 None 表示可以继续，否则是终止原因。

        - 判定：估算 > context_window - reserve_tokens；窗口未知、未超阈值都放行
        - 需要在压缩时执行一次 M7.5 事务，保留预算与手动 `/compact` 相同
        - 摘要失败、写盘失败与无安全切点都返回原因，不吞掉也不落半成品
        - 压缩后仍超阈值同样不能继续：那会越过模型窗口，必须显式结束这次 run
        - 两个调用点共用：`run()` 的 prompt 前检查与工具轮之间的钩子
        """
        policy = resolve_policy(self._model)
        if policy is None:
            # 窗口未知：不猜百分比、不做自动压缩，也不阻止任务
            return None
        estimate = estimate_tokens(self._agent.state.messages)
        if evaluate_compaction(estimate, policy=policy).status != "needed":
            return None
        try:
            execution = self.compact(keep_recent_tokens=DEFAULT_KEEP_RECENT_TOKENS)
        except (MiniPiError, OSError) as exc:
            # 摘要调用（LLMError / CompactionError）与写盘失败：事务已保证
            # JSONL 与内存投影都不变，这里只负责把它转成终止原因
            return _compaction_failure(str(exc))
        if execution.result is None:
            # 需要压缩却没有安全切点或没有新内容可摘要：不能携超限上下文继续
            return _compaction_failure(execution.reason)
        after = estimate_tokens(self._agent.state.messages)
        if after.tokens > policy.threshold_tokens:
            # 单个工具轮本身就超过阈值：压缩已尽力，仍不得发出越窗请求
            return _compaction_failure(
                f"compaction left {after.tokens} tokens above the window threshold "
                f"{policy.threshold_tokens} ({after.source})"
            )
        return None

    def _compact_between_turns(self) -> None:
        """工具轮之间的钩子：窗口触发失败必须终止，成本触发只是尽力而为。

        钩子没有返回值通道，窗口触发的可预期失败用 CompactionError 表达；Loop 会把它
        转成 `AgentEndEvent(reason="error")` 与 error assistant 消息，已提交的工具结果保留。
        """
        failure = self._auto_compact_if_needed()
        if failure is not None:
            raise CompactionError(failure)
        self._compact_old_tool_results()

    def _compact_old_tool_results(self) -> None:
        """窗口内但旧工具结果占主导时，按 M7.6f 成本模型提前压缩一次。

        - 只在工具轮之间尝试：这里确定还会有下一次请求，节省才有对象
        - 窗口未知时不做任何自动压缩（沿用 M7.4g 边界），成本触发也不例外
        - 判定与成本估算都是纯函数；不通过判定就不会调用摘要模型
        - 每次 run 最多一次；摘要阶段失败只放弃这次优化，不改变本次 run 的结果
        - 写盘或重建失败会让内存投影与 JSONL 分叉，与窗口触发同样终止这次 run
        """
        if self._cost_compaction_attempted or resolve_policy(self._model) is None:
            return
        preparation = prepare_compaction(
            self._session.active_entries(), keep_recent_tokens=DEFAULT_KEEP_RECENT_TOKENS
        )
        if preparation.plan is None:
            # 没有安全切点或没有新内容：提前压缩没有对象，也不该报错
            return
        plan = preparation.plan
        cost = estimate_compaction_cost(plan)
        decision = decide_cost_aware_compaction(
            cost, measure_tool_results(plan.messages_to_summarize)
        )
        if not decision.should_compact:
            return
        # 先置位再尝试：失败后本次 run 不再重试，避免每个工具轮都浪费一次调用
        self._cost_compaction_attempted = True
        try:
            self._commit_prepared(preparation)
        except (LLMError, CompactionError):
            # 摘要请求失败时事务尚未写盘：窗口内继续请求是安全的
            return
        except (SessionError, OSError) as exc:
            # 写盘/重建失败已越过“只是优化失败”的边界：不得让投影与 JSONL 分叉
            raise CompactionError(_compaction_failure(str(exc))) from exc

    def _abort_before_prompt(self, error: str) -> AssistantMessage:
        """prompt 前的自动压缩失败：发出成对的 start/end 事件并返回 error 消息。

        这次任务没有开始，因此不写任何 entry；返回的 assistant 消息只用于表达终止，
        CLI 依据 `AgentEndEvent.reason == "error"` 显示原因。
        """
        if self._on_event is not None:
            self._on_event(AgentStartEvent())
            self._on_event(AgentEndEvent(reason="error", error=error))
        return AssistantMessage(stop_reason="error", error_message=error)

    def compact(
        self, *, keep_recent_tokens: int, instructions: str | None = None
    ) -> CompactionExecution:
        """对当前投影执行一次手动压缩；没有安全切点时不做任何改动。

        - 摘要调用走当前会话的 LLM，失败（LLMError / CompactionError）直接冒泡
        - `instructions` 只进入本次摘要请求，不写进 Session
        - 只有摘要成功后才写 CompactionEntry，再从 entry 路径重建内存投影
        """
        preparation = prepare_compaction(
            self._session.active_entries(), keep_recent_tokens=keep_recent_tokens
        )
        return self._commit_prepared(preparation, instructions=instructions)

    def _commit_prepared(
        self, preparation: CompactionPreparation, *, instructions: str | None = None
    ) -> CompactionExecution:
        """执行已备好的压缩：摘要成功后才写 entry 并重建内存投影。"""
        if preparation.plan is None:
            return CompactionExecution(result=None, reason=preparation.reason)
        result = generate_compaction_result(
            preparation.plan, self._llm, instructions=instructions
        )
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
