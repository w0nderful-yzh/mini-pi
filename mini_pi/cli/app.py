"""CLI 入口：一次性任务与交互式 REPL，支持 /connect 配置凭据。"""

from __future__ import annotations

import sys
from importlib import metadata
from pathlib import Path

import typer
from rich.console import Console
from rich.prompt import Prompt

from mini_pi.agent.agent import Agent
from mini_pi.auth import (
    ConnectionPreference,
    load_last_connection,
    resolve_api_key,
    save_connection,
    save_last_connection,
)
from mini_pi.cli.banner import render_banner, render_startup
from mini_pi.cli.console import ConsoleRenderer
from mini_pi.cli.input import create_repl_reader
from mini_pi.cli.sessions import render_sessions
from mini_pi.cli.status import (
    current_context_tokens,
    render_compaction,
    render_context,
    render_status,
    render_tools,
)
from mini_pi.context.policy import DEFAULT_KEEP_RECENT_TOKENS
from mini_pi.errors import MiniPiError, MissingAPIKeyError, SessionError
from mini_pi.llm.base import LLMClient
from mini_pi.llm.deepseek_client import DeepSeekClient
from mini_pi.llm.openai_client import OpenAIClient
from mini_pi.llm.types import UserMessage
from mini_pi.session.jsonl import latest_session_path
from mini_pi.session.runtime import AgentSession
from mini_pi.tools import build_default_registry
from mini_pi.workspace.workspace import Workspace

app = typer.Typer(add_completion=False, help="mini-pi: a lightweight Python coding agent")

PROVIDERS: tuple[str, ...] = ("openai", "deepseek")
DEFAULT_MODELS = {"openai": "gpt-5.6-terra", "deepseek": "deepseek-flash"}
API_KEY_ENV = {"openai": "OPENAI_API_KEY", "deepseek": "DEEPSEEK_API_KEY"}

_HELP_TEXT = """Available commands:
  /model [provider] [model]  switch provider/model (reuse saved key; ask only if missing)
  /compact [instructions]    summarize older context into a checkpoint (saved sessions only)
  /sessions                  list validated sessions for this workspace
  /new                       start a new session (saved sessions only)
  /reset                     clear in-memory context (memory-only sessions)
  /status [full]             show model, workspace, session, and context
  /context                   show estimated context categories
  /tools                     list available tools
  /help                      show this help
  /exit                      quit mini-pi

Input: Enter submits, Ctrl+J or Alt+Enter starts a new line, Ctrl+L clears the screen.
Ctrl+C cancels the running task; press it twice in a row to exit.

/connect is kept as an alias of /model."""


def _version() -> str:
    """读取已安装包版本；源码直跑或未打包时回退到 0.0.0。"""
    try:
        return metadata.version("mini-pi")
    except metadata.PackageNotFoundError:
        return "0.0.0"


def create_llm(provider: str, model: str | None = None, *, api_key: str | None = None) -> LLMClient:
    """按 provider 构造客户端；Key 解析顺序为显式参数 > 环境变量 > auth.json。"""
    if provider not in PROVIDERS:
        raise typer.BadParameter(f"unsupported provider: {provider!r} (expected 'openai' or 'deepseek')")
    env_var = API_KEY_ENV[provider]
    resolved_key = api_key if api_key is not None else resolve_api_key(provider, env_var=env_var)
    if not resolved_key:
        raise MissingAPIKeyError(
            f"{provider} API key is not configured; set {env_var} or use /model (/connect)"
        )
    resolved_model = model or DEFAULT_MODELS[provider]
    if provider == "openai":
        return OpenAIClient(model=resolved_model, api_key=resolved_key)
    return DeepSeekClient(model=resolved_model, api_key=resolved_key)


def _has_key(provider: str) -> bool:
    """环境变量或 auth.json 中存在该 provider 的可用 Key。"""
    return resolve_api_key(provider, env_var=API_KEY_ENV[provider]) is not None


def _configured_keys() -> list[str]:
    """收集当前可用的 Provider 凭据，仅供终端输出脱敏。"""
    return [
        value
        for candidate in PROVIDERS
        if (value := resolve_api_key(candidate, env_var=API_KEY_ENV[candidate])) is not None
    ]


def _detect_provider(previous: ConnectionPreference | None) -> str:
    """无显式参数时选 provider：上次连接有 Key 才认，否则选第一个已配 Key 的。"""
    if previous is not None and _has_key(previous.provider):
        return previous.provider
    for candidate in PROVIDERS:
        if _has_key(candidate):
            return candidate
    return previous.provider if previous is not None else "openai"


def ask_api_key(console: Console, provider: str) -> str | None:
    """隐藏输入 API Key；空输入视为取消。"""
    value = Prompt.ask(f"{provider} API key", password=True, console=console).strip()
    return value or None


def ask_model_choice(
    console: Console, *, provider: str, model: str
) -> tuple[str, str] | None:
    """询问目标 provider/model；模型为空视为取消。"""
    chosen_provider = Prompt.ask(
        "Provider", choices=list(PROVIDERS), default=provider, console=console
    )
    default_model = model if chosen_provider == provider else DEFAULT_MODELS[chosen_provider]
    chosen_model = Prompt.ask("Model", default=default_model, console=console).strip()
    if not chosen_model:
        return None
    return chosen_provider, chosen_model


def verify_credentials(provider: str, api_key: str, model: str | None) -> None:
    """用一次最小真实请求验证 Key；失败抛 MiniPiError（含 LLMError）。"""
    llm = create_llm(provider, model, api_key=api_key)
    llm.complete([UserMessage(content="Reply with OK")])


def _resolve_connection(
    provider: str | None, model: str | None
) -> tuple[str, str]:
    """按显式参数、可用凭据、上次选择、内置默认值的顺序解析启动配置。"""
    previous = load_last_connection()
    resolved_provider = provider or _detect_provider(previous)
    if resolved_provider not in PROVIDERS:
        if provider is None:
            raise MiniPiError(
                f"unsupported provider {resolved_provider!r} in saved connection"
            )
        raise typer.BadParameter(
            f"unsupported provider: {resolved_provider!r} (expected 'openai' or 'deepseek')"
        )
    previous_model = (
        previous.model
        if previous is not None and previous.provider == resolved_provider
        else None
    )
    return resolved_provider, model or previous_model or DEFAULT_MODELS[resolved_provider]


def _prompt_and_build(
    *,
    console: Console,
    workspace: Workspace,
    max_steps: int,
    max_run_input_tokens: int | None = None,
    renderer: ConsoleRenderer,
    provider: str,
    model: str,
    no_session: bool,
) -> Agent | AgentSession | None:
    """启动阶段补 Key：隐藏输入 → 单次验证 → 原子保存 → 装配 Runtime。"""
    api_key = ask_api_key(console, provider)
    if api_key is None:
        return None
    try:
        verify_credentials(provider, api_key, model)
    except MiniPiError as exc:
        console.print(f"key verification failed: {exc}", style="red", markup=False)
        return None
    save_connection(provider, api_key, model)
    return _build_runtime(
        llm=create_llm(provider, model, api_key=api_key),
        workspace=workspace,
        max_steps=max_steps,
        max_run_input_tokens=max_run_input_tokens,
        renderer=renderer,
        provider=provider,
        model=model,
        no_session=no_session,
    )


def _build_agent(
    *,
    llm: LLMClient,
    workspace: Workspace,
    max_steps: int,
    max_run_input_tokens: int | None = None,
    renderer: ConsoleRenderer,
) -> Agent:
    """装配纯内存 Agent（--no-session）；事件走 renderer，不接持久化。"""
    return Agent(
        llm=llm,
        registry=build_default_registry(workspace),
        cwd=workspace.root,
        max_steps=max_steps,
        max_run_input_tokens=max_run_input_tokens,
        on_event=renderer.handle,
    )


def _build_runtime(
    *,
    llm: LLMClient,
    workspace: Workspace,
    max_steps: int,
    max_run_input_tokens: int | None = None,
    renderer: ConsoleRenderer,
    provider: str,
    model: str,
    no_session: bool,
) -> Agent | AgentSession:
    """按 CLI 模式装配纯内存 Agent 或持久化 AgentSession。"""
    if no_session:
        return _build_agent(
            llm=llm,
            workspace=workspace,
            max_steps=max_steps,
            max_run_input_tokens=max_run_input_tokens,
            renderer=renderer,
        )
    return AgentSession.create(
        cwd=workspace.root,
        llm=llm,
        registry=build_default_registry(workspace),
        provider=provider,
        model=model,
        max_steps=max_steps,
        max_run_input_tokens=max_run_input_tokens,
        on_event=renderer.handle,
    )


def _is_model_command(command: str) -> bool:
    """`/model` 与兼容别名 `/connect`；两者都支持 provider/model 参数。"""
    return command.split(maxsplit=1)[0] in {"/model", "/connect"}


def _run_compaction(
    console: Console,
    agent: Agent | AgentSession | None,
    *,
    model: str,
    instructions: str | None,
) -> None:
    """执行一次手动压缩：显示前后估算、切点与摘要调用成本，失败不中断 REPL。"""
    if agent is None:
        console.print("configure a provider first with /model", style="yellow")
        return
    if not isinstance(agent, AgentSession):
        # --no-session 不能隐式建 JSONL：压缩检查点必须有可追加的事实源
        console.print(
            "/compact requires a saved session; restart without --no-session",
            style="yellow",
        )
        return
    before = current_context_tokens(agent)
    try:
        execution = agent.compact(
            keep_recent_tokens=DEFAULT_KEEP_RECENT_TOKENS, instructions=instructions
        )
    except MiniPiError as exc:
        console.print(f"compaction failed: {exc}", style="red", markup=False)
        return
    render_compaction(
        console,
        model=model,
        execution=execution,
        tokens_before=before,
        tokens_after=current_context_tokens(agent),
    )


def _switch_connection(
    *,
    console: Console,
    agent: Agent | AgentSession | None,
    provider: str,
    model: str,
    workspace: Workspace,
    max_steps: int,
    max_run_input_tokens: int | None = None,
    renderer: ConsoleRenderer,
    no_session: bool,
    arguments: list[str],
) -> tuple[Agent | AgentSession, str, str] | None:
    """切换 provider/model：已有 Key 直接复用，缺失时才隐藏输入并验证。"""
    if arguments:
        if len(arguments) == 1:
            chosen_provider = arguments[0]
            # 同 provider 只换模型名时沿用当前模型，避免悄悄切回内置默认
            chosen_model = model if chosen_provider == provider else DEFAULT_MODELS.get(chosen_provider, "")
        elif len(arguments) == 2:
            chosen_provider, chosen_model = arguments
        else:
            console.print("usage: /model [provider] [model]", style="yellow", markup=False)
            return None
        if chosen_provider not in PROVIDERS:
            console.print(
                f"unsupported provider: {chosen_provider}", style="red", markup=False
            )
            return None
        if not chosen_model:
            console.print("model must not be empty", style="red", markup=False)
            return None
    else:
        choice = ask_model_choice(console, provider=provider, model=model)
        if choice is None:
            console.print("model switch cancelled", style="yellow", markup=False)
            return None
        chosen_provider, chosen_model = choice

    api_key = resolve_api_key(chosen_provider, env_var=API_KEY_ENV[chosen_provider])
    entered_key: str | None = None
    if api_key is None:
        entered_key = ask_api_key(console, chosen_provider)
        if entered_key is None:
            console.print(
                "model switch cancelled: no API key provided", style="yellow", markup=False
            )
            return None
        api_key = entered_key

    try:
        if entered_key is not None:
            # 只有新输入的 Key 需要真实请求验证；已保存的 Key 直接复用
            verify_credentials(chosen_provider, api_key, chosen_model)
        new_llm = create_llm(chosen_provider, chosen_model, api_key=api_key)
        if agent is None:
            new_agent: Agent | AgentSession = _build_runtime(
                llm=new_llm,
                workspace=workspace,
                max_steps=max_steps,
                max_run_input_tokens=max_run_input_tokens,
                renderer=renderer,
                provider=chosen_provider,
                model=chosen_model,
                no_session=no_session,
            )
            if isinstance(new_agent, AgentSession):
                console.print(f"session: {new_agent.session_id[:8]}", markup=False)
        else:
            new_agent = agent
            if isinstance(new_agent, AgentSession):
                new_agent.set_llm(new_llm, provider=chosen_provider, model=chosen_model)
            else:
                new_agent.set_llm(new_llm)
    except MiniPiError as exc:
        console.print(f"model switch failed: {exc}", style="red", markup=False)
        return None

    if entered_key is not None:
        auth_path = save_connection(chosen_provider, entered_key, chosen_model)
        console.print(f"saved key to {auth_path}", soft_wrap=True)
    else:
        save_last_connection(chosen_provider, chosen_model)
    console.print(
        f"model: {chosen_provider}/{chosen_model}", style="green", markup=False, soft_wrap=True
    )
    return new_agent, chosen_provider, chosen_model


@app.command()
def cli(
    prompt: str | None = typer.Argument(
        None, help="Task to run once; omit to start an interactive session."
    ),
    provider: str | None = typer.Option(None, "--provider", "-p"),
    model: str | None = typer.Option(None, "--model", "-m"),
    cwd: Path = typer.Option(Path("."), "--cwd"),
    max_steps: int = typer.Option(50, "--max-steps", min=1),
    max_run_input_tokens: int | None = typer.Option(
        None,
        "--max-run-input-tokens",
        min=1,
        help="Opt-in cumulative input budget per task, checked before each model request.",
    ),
    no_session: bool = typer.Option(False, "--no-session", help="Keep history in memory only."),
    no_banner: bool = typer.Option(False, "--no-banner", help="Do not print the startup banner."),
    verbose: bool = typer.Option(False, "--verbose", help="Show tool arguments and bounded logs."),
    resume: Path | None = typer.Option(None, "--resume", help="Resume a Session JSONL file."),
    continue_session: bool = typer.Option(
        False, "--continue", help="Resume the latest Session for this workspace."
    ),
) -> None:
    """主命令：解析选项后一次性跑 prompt 或进入交互式 REPL。"""
    if resume is not None and continue_session:
        raise typer.BadParameter("--resume and --continue are mutually exclusive")
    if no_session and (resume is not None or continue_session):
        raise typer.BadParameter("--no-session cannot be used with --resume or --continue")

    workspace = Workspace(cwd)
    console = Console()
    renderer = ConsoleRenderer(console, show_thinking=not no_banner, verbose=verbose)
    agent: Agent | AgentSession | None = None
    startup_error: str | None = None
    config_error: MiniPiError | None = None
    if resume is not None or continue_session:
        try:
            path = resume if resume is not None else latest_session_path(workspace.root)
            agent = AgentSession.resume(
                path,
                registry=build_default_registry(workspace),
                cwd=workspace.root,
                provider=provider,
                model=model,
                llm_factory=create_llm,
                max_steps=max_steps,
                max_run_input_tokens=max_run_input_tokens,
                on_event=renderer.handle,
            )
        except MiniPiError as exc:
            Console(stderr=True).print(f"error: {exc}", style="red", soft_wrap=True)
            raise typer.Exit(code=1) from exc
    else:
        try:
            provider, model = _resolve_connection(provider, model)
            new_agent = _build_runtime(
                llm=create_llm(provider, model),
                workspace=workspace,
                max_steps=max_steps,
                max_run_input_tokens=max_run_input_tokens,
                renderer=renderer,
                provider=provider,
                model=model,
                no_session=no_session,
            )
            agent = new_agent
            # 记住本次成功使用的 provider/model，下次启动优先复用
            save_last_connection(provider, model)
        except MiniPiError as exc:
            config_error = exc
            startup_error = (
                f"session creation failed: {exc}"
                if not no_session and isinstance(exc, SessionError)
                else str(exc)
            )

    # 解析失败时仍给 /connect 一个确定默认值；恢复成功则以活动链的模型为准。
    provider = provider or "openai"
    model = model or DEFAULT_MODELS.get(provider, DEFAULT_MODELS["openai"])
    if isinstance(agent, AgentSession):
        provider, model = agent.provider, agent.model
    if agent is not None:
        renderer.set_secrets(_configured_keys())

    if prompt is not None:
        if agent is None:
            Console(stderr=True).print(f"error: {startup_error}", style="red")
            raise typer.Exit(code=1)
        if isinstance(agent, AgentSession):
            console.print(f"Session storage: {agent.path.parent}", soft_wrap=True)
        try:
            agent.run(prompt)
            if renderer.last_end_reason == "budget_limit":
                # 一次性调用未完成必须返回非零；Session 已保留，可继续恢复。
                raise typer.Exit(code=2)
            if renderer.last_end_reason == "cancelled":
                # 用户中断按 128+SIGINT 退出；已提交的消息与改动仍在 Session 中
                raise typer.Exit(code=130)
            if renderer.last_end_reason == "error":
                # Agent 以 agent error 结束（含自动压缩失败）同样不能伪装成成功
                raise typer.Exit(code=1)
        except KeyboardInterrupt:
            # 中断可能落在渲染边界之外：按同一约定退出，不伪装成成功
            Console(stderr=True).print("interrupted", style="yellow")
            raise typer.Exit(code=130) from None
        except MiniPiError as exc:
            # 一次性任务失败以非零码退出，避免把可预期错误伪装成成功
            Console(stderr=True).print(f"error: {exc}", style="red", soft_wrap=True)
            raise typer.Exit(code=1) from exc
        finally:
            renderer.close()
            if isinstance(agent, AgentSession):
                console.print(f"Session path: {agent.path}", soft_wrap=True)
        return

    render_banner(console, enabled=not no_banner)
    if (
        agent is None
        and isinstance(config_error, MissingAPIKeyError)
        and sys.stdin.isatty()
    ):
        # 交互启动缺 Key：直接提示输入，免去用户记命令的成本
        agent = _prompt_and_build(
            console=console,
            workspace=workspace,
            max_steps=max_steps,
            max_run_input_tokens=max_run_input_tokens,
            renderer=renderer,
            provider=provider,
            model=model,
            no_session=no_session,
        )
        if agent is not None:
            startup_error = None
            renderer.set_secrets(_configured_keys())
    session_label = (
        agent.session_id[:8]
        if isinstance(agent, AgentSession)
        else "memory" if isinstance(agent, Agent) else "not connected"
    )
    render_startup(
        console,
        version=_version(),
        provider=provider,
        model=model,
        project=workspace.root.name or str(workspace.root),
        session=session_label,
    )
    if startup_error is not None:
        console.print(
            startup_error,
            style="red" if startup_error.startswith("session creation failed:") else "yellow",
        )

    reader = create_repl_reader()
    # 连续 Ctrl+C 计数：第一次取消当前输入或任务，连续第二次才退出进程
    interrupt_streak = 0
    while True:
        try:
            line = reader.read()
        except EOFError:
            break
        except KeyboardInterrupt:
            interrupt_streak += 1
            if interrupt_streak >= 2:
                console.print("exiting mini-pi.", markup=False)
                break
            console.print("Press Ctrl+C again to exit.", style="yellow", markup=False)
            continue
        except UnicodeDecodeError as exc:
            # 终端字节不是有效 UTF-8：拒绝并提示，避免代理项污染 Session
            console.print(
                f"invalid input encoding: {exc}; check terminal encoding (LANG/LC_CTYPE)",
                style="red",
            )
            continue
        # 成功提交一次输入即清零：只有连续中断才退出
        interrupt_streak = 0
        stripped = line.strip()
        if stripped in {"/exit", "/quit"}:
            break
        if stripped == "/help":
            console.print(_HELP_TEXT, markup=False)
            continue
        if stripped in {"/status", "/status full"}:
            render_status(
                console,
                agent=agent,
                provider=provider,
                model=model,
                cwd=workspace.root,
                renderer=renderer,
                full=stripped.endswith(" full"),
            )
            continue
        if stripped == "/context":
            render_context(console, agent=agent, model=model, renderer=renderer)
            continue
        if stripped == "/tools":
            render_tools(console, agent=agent, cwd=workspace.root)
            continue
        if stripped == "/sessions":
            try:
                render_sessions(
                    console,
                    cwd=workspace.root,
                    current_session_id=(
                        agent.session_id if isinstance(agent, AgentSession) else None
                    ),
                )
            except SessionError as exc:
                console.print(f"session listing failed: {exc}", style="red", markup=False)
            continue
        if stripped == "/compact" or stripped.startswith("/compact "):
            # 可选 instructions 只随本次摘要请求发送，不写入 Session
            _run_compaction(
                console,
                agent,
                model=model,
                instructions=stripped[len("/compact") :].strip() or None,
            )
            continue
        if stripped == "/new":
            if isinstance(agent, AgentSession):
                try:
                    new_agent = agent.new()
                except SessionError as exc:
                    console.print(f"session creation failed: {exc}", style="red")
                else:
                    agent = new_agent
                    console.print(f"new session: {agent.session_id[:8]}", markup=False)
            elif agent is None:
                console.print("configure a provider first with /model", style="yellow")
            else:
                console.print("/new requires a saved session; use /reset", style="yellow")
            continue
        if stripped == "/reset":
            if isinstance(agent, AgentSession):
                console.print(
                    "/reset is unavailable for saved sessions; use /new",
                    style="yellow",
                )
            elif agent is not None:
                agent.reset()
                console.print("context cleared")
            continue
        if _is_model_command(stripped):
            switched = _switch_connection(
                console=console,
                agent=agent,
                provider=provider,
                model=model,
                workspace=workspace,
                max_steps=max_steps,
                max_run_input_tokens=max_run_input_tokens,
                renderer=renderer,
                no_session=no_session,
                arguments=stripped.split()[1:],
            )
            if switched is not None:
                agent, provider, model = switched
                renderer.set_secrets(_configured_keys())
            continue
        if not stripped:
            continue
        if stripped.startswith("/"):
            # 未知命令不能当任务发给模型，否则会把斜杠文本写进 Session
            console.print(
                f"unknown command: {stripped} — type /help", style="yellow", markup=False
            )
            continue
        if agent is None:
            console.print("configure a provider first with /model", style="yellow")
            continue
        try:
            agent.run(stripped)
        except KeyboardInterrupt:
            # Loop 已在流式/工具边界把中断转成 cancelled；这里只在渲染边界兜底
            console.print("interrupted", style="yellow")
            interrupt_streak = 1
        except Exception as exc:
            # REPL 顶层边界：程序缺陷要完整可见，但不因此终止整个会话
            console.print(f"unexpected error: {type(exc).__name__}: {exc}", style="red")
            console.print_exception()
        finally:
            renderer.close()
        if renderer.last_end_reason == "cancelled":
            # 任务刚被 Ctrl+C 取消：下一次空闲 Ctrl+C 直接退出，符合连续两次语义
            interrupt_streak = 1


def _force_utf8(stream: object, *, errors: str) -> None:
    """把文本流重配为 UTF-8，使 stdio 不依赖进程 locale。"""
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        # 测试捕获流或管道可能不支持重配，保持现状
        return
    try:
        reconfigure(encoding="utf-8", errors=errors)
    except (ValueError, OSError):
        # 已开始读写的流无法重配时退化为现状；业务错误不受影响
        return


def _configure_stdio() -> None:
    """入口统一 stdio 编码：输入严格校验，输出转义保证展示不中断。"""
    _force_utf8(sys.stdin, errors="strict")
    _force_utf8(sys.stdout, errors="backslashreplace")
    _force_utf8(sys.stderr, errors="backslashreplace")


def main() -> None:
    """入口：先把 stdio 强制为 UTF-8，再交给 Typer app。"""
    _configure_stdio()
    app()
