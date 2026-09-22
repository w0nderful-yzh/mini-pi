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
    load_last_connection,
    resolve_api_key,
    save_connection,
    save_last_connection,
)
from mini_pi.cli.banner import render_banner
from mini_pi.cli.console import ConsoleRenderer
from mini_pi.errors import MiniPiError, SessionError
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
  /connect  configure provider, API key and model
  /new      start a new session (saved sessions only)
  /reset    clear in-memory context (memory-only sessions)
  /help     show this help
  /exit     quit mini-pi"""


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
        raise MiniPiError(
            f"{provider} API key is not configured; set {env_var} or run mini-pi and use /connect"
        )
    resolved_model = model or DEFAULT_MODELS[provider]
    if provider == "openai":
        return OpenAIClient(model=resolved_model, api_key=resolved_key)
    return DeepSeekClient(model=resolved_model, api_key=resolved_key)


def ask_credentials(console: Console, default_provider: str) -> tuple[str, str] | None:
    """交互式询问 provider 与 Key；空 Key 视为取消。"""
    provider = Prompt.ask(
        "Provider", choices=list(PROVIDERS), default=default_provider, console=console
    )
    api_key = Prompt.ask(f"{provider} API key", password=True, console=console).strip()
    if not api_key:
        return None
    return provider, api_key


def verify_credentials(provider: str, api_key: str, model: str | None) -> None:
    """用一次最小真实请求验证 Key；失败抛 MiniPiError（含 LLMError）。"""
    llm = create_llm(provider, model, api_key=api_key)
    llm.complete([UserMessage(content="Reply with OK")])


def _resolve_connection(
    provider: str | None, model: str | None
) -> tuple[str, str]:
    """按显式参数、上次选择、内置默认值的顺序解析启动配置。"""
    previous = load_last_connection()
    resolved_provider = provider or (previous.provider if previous is not None else "openai")
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


def _build_agent(
    *, llm: LLMClient, workspace: Workspace, max_steps: int, renderer: ConsoleRenderer
) -> Agent:
    return Agent(
        llm=llm,
        registry=build_default_registry(workspace),
        cwd=workspace.root,
        max_steps=max_steps,
        on_event=renderer.handle,
    )


def _build_runtime(
    *,
    llm: LLMClient,
    workspace: Workspace,
    max_steps: int,
    renderer: ConsoleRenderer,
    provider: str,
    model: str,
    no_session: bool,
) -> Agent | AgentSession:
    """按 CLI 模式装配纯内存 Agent 或持久化 AgentSession。"""
    if no_session:
        return _build_agent(llm=llm, workspace=workspace, max_steps=max_steps, renderer=renderer)
    return AgentSession.create(
        cwd=workspace.root,
        llm=llm,
        registry=build_default_registry(workspace),
        provider=provider,
        model=model,
        max_steps=max_steps,
        on_event=renderer.handle,
    )


@app.command()
def cli(
    prompt: str | None = typer.Argument(
        None, help="Task to run once; omit to start an interactive session."
    ),
    provider: str | None = typer.Option(None, "--provider", "-p"),
    model: str | None = typer.Option(None, "--model", "-m"),
    cwd: Path = typer.Option(Path("."), "--cwd"),
    max_steps: int = typer.Option(50, "--max-steps", min=1),
    no_session: bool = typer.Option(False, "--no-session", help="Keep history in memory only."),
    no_banner: bool = typer.Option(False, "--no-banner", help="Do not print the startup banner."),
    resume: Path | None = typer.Option(None, "--resume", help="Resume a Session JSONL file."),
    continue_session: bool = typer.Option(
        False, "--continue", help="Resume the latest Session for this workspace."
    ),
) -> None:
    if resume is not None and continue_session:
        raise typer.BadParameter("--resume and --continue are mutually exclusive")
    if no_session and (resume is not None or continue_session):
        raise typer.BadParameter("--no-session cannot be used with --resume or --continue")

    workspace = Workspace(cwd)
    console = Console()
    renderer = ConsoleRenderer(console)
    agent: Agent | AgentSession | None = None
    startup_error: str | None = None
    provider_override = provider
    model_override = model
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
                renderer=renderer,
                provider=provider,
                model=model,
                no_session=no_session,
            )
            # 显式选择代表用户更新默认项；从已保存配置启动时无需重复写盘。
            if provider_override is not None or model_override is not None:
                save_last_connection(provider, model)
            agent = new_agent
        except MiniPiError as exc:
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

    if prompt is not None:
        if agent is None:
            Console(stderr=True).print(f"error: {startup_error}", style="red")
            raise typer.Exit(code=1)
        if isinstance(agent, AgentSession):
            console.print(f"Session storage: {agent.path.parent}", soft_wrap=True)
        try:
            agent.run(prompt)
        except MiniPiError as exc:
            # 一次性任务失败以非零码退出，避免把可预期错误伪装成成功
            Console(stderr=True).print(f"error: {exc}", style="red", soft_wrap=True)
            raise typer.Exit(code=1) from exc
        finally:
            if isinstance(agent, AgentSession):
                console.print(f"Session path: {agent.path}", soft_wrap=True)
        return

    commands = "/connect, /reset, /help, /exit" if no_session else "/connect, /new, /reset, /help, /exit"
    render_banner(console, enabled=not no_banner)
    console.print(
        f"  mini-pi {_version()} · {provider}/{model} · {workspace.root}",
        style="dim",
        markup=False,
        soft_wrap=True,
    )
    console.print(f"  commands: {commands}", style="dim", markup=False, soft_wrap=True)
    if isinstance(agent, AgentSession):
        console.print(f"Session storage: {agent.path.parent}", soft_wrap=True)
    if startup_error is not None:
        console.print(
            startup_error,
            style="red" if startup_error.startswith("session creation failed:") else "yellow",
        )

    while True:
        try:
            line = input("mini-pi> ")
        except (EOFError, KeyboardInterrupt):
            break
        except UnicodeDecodeError as exc:
            # 终端字节不是有效 UTF-8：拒绝并提示，避免代理项污染 Session
            console.print(
                f"invalid input encoding: {exc}; check terminal encoding (LANG/LC_CTYPE)",
                style="red",
            )
            continue
        stripped = line.strip()
        if stripped in {"/exit", "/quit"}:
            break
        if stripped == "/help":
            console.print(_HELP_TEXT, markup=False)
            continue
        if stripped == "/new":
            if isinstance(agent, AgentSession):
                try:
                    new_agent = agent.new()
                except SessionError as exc:
                    console.print(f"session creation failed: {exc}", style="red")
                else:
                    agent = new_agent
                    console.print(f"new session: {agent.path}", soft_wrap=True)
            elif agent is None:
                console.print("configure a provider first with /connect", style="yellow")
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
        if stripped == "/connect":
            credentials = ask_credentials(console, provider)
            if credentials is None:
                console.print("connect cancelled", style="yellow")
                continue
            new_provider, api_key = credentials
            new_model = model if new_provider == provider else DEFAULT_MODELS[new_provider]
            try:
                verify_credentials(new_provider, api_key, new_model)
                # 验证成功后先构造客户端，避免配置已保存但当前进程无法切换。
                new_llm = create_llm(new_provider, new_model, api_key=api_key)
            except MiniPiError as exc:
                console.print(f"key verification failed: {exc}", style="red")
                continue
            auth_path = save_connection(new_provider, api_key, new_model)
            provider = new_provider
            model = new_model
            # 当前进程继续使用刚验证的 Key；环境变量优先级在下次启动时再生效。
            if agent is None:
                try:
                    agent = _build_runtime(
                        llm=new_llm,
                        workspace=workspace,
                        max_steps=max_steps,
                        renderer=renderer,
                        provider=provider,
                        model=model,
                        no_session=no_session,
                    )
                except SessionError as exc:
                    console.print(f"session creation failed: {exc}", style="red")
                    continue
                if isinstance(agent, AgentSession):
                    console.print(f"Session storage: {agent.path.parent}", soft_wrap=True)
            elif isinstance(agent, AgentSession):
                agent.set_llm(new_llm, provider=provider, model=model)
            else:
                agent.set_llm(new_llm)
            console.print(f"saved to {auth_path}")
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
            console.print("configure a provider first with /connect", style="yellow")
            continue
        try:
            agent.run(stripped)
        except KeyboardInterrupt:
            # 只中断当前任务，不退出交互
            console.print("interrupted", style="yellow")
        except Exception as exc:
            # REPL 顶层边界：程序缺陷要完整可见，但不因此终止整个会话
            console.print(f"unexpected error: {type(exc).__name__}: {exc}", style="red")
            console.print_exception()

    if isinstance(agent, AgentSession):
        console.print(f"Session path: {agent.path}", soft_wrap=True)


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
    _configure_stdio()
    app()
