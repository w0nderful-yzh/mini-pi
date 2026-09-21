"""CLI 入口：一次性任务与交互式 REPL，支持 /connect 配置凭据。"""

from __future__ import annotations

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
from mini_pi.cli.console import ConsoleRenderer
from mini_pi.errors import MiniPiError
from mini_pi.llm.base import LLMClient
from mini_pi.llm.deepseek_client import DeepSeekClient
from mini_pi.llm.openai_client import OpenAIClient
from mini_pi.llm.types import UserMessage
from mini_pi.tools import build_default_registry
from mini_pi.workspace.workspace import Workspace

app = typer.Typer(add_completion=False, help="mini-pi: a lightweight Python coding agent")

PROVIDERS: tuple[str, ...] = ("openai", "deepseek")
DEFAULT_MODELS = {"openai": "gpt-4o-mini", "deepseek": "deepseek-chat"}
API_KEY_ENV = {"openai": "OPENAI_API_KEY", "deepseek": "DEEPSEEK_API_KEY"}


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


@app.command()
def cli(
    prompt: str | None = typer.Argument(None, help="Task to run once; omit to start an interactive session."),
    provider: str | None = typer.Option(None, "--provider", "-p"),
    model: str | None = typer.Option(None, "--model", "-m"),
    cwd: Path = typer.Option(Path("."), "--cwd"),
    max_steps: int = typer.Option(50, "--max-steps", min=1),
) -> None:
    workspace = Workspace(cwd)
    console = Console()
    renderer = ConsoleRenderer(console)
    agent: Agent | None = None
    startup_error: str | None = None
    provider_override = provider
    model_override = model
    try:
        provider, model = _resolve_connection(provider, model)
        new_agent = _build_agent(
            llm=create_llm(provider, model),
            workspace=workspace,
            max_steps=max_steps,
            renderer=renderer,
        )
        # 显式选择代表用户更新默认项；从已保存配置启动时无需重复写盘。
        if provider_override is not None or model_override is not None:
            save_last_connection(provider, model)
        agent = new_agent
    except MiniPiError as exc:
        startup_error = str(exc)

    # 解析失败时仍给 /connect 一个确定默认值，避免交互层处理 Optional。
    provider = provider or "openai"
    model = model or DEFAULT_MODELS.get(provider, DEFAULT_MODELS["openai"])

    if prompt is not None:
        if agent is None:
            typer.echo(f"error: {startup_error}", err=True)
            raise typer.Exit(code=1)
        agent.run(prompt)
        return

    console.print("mini-pi interactive mode. Commands: /connect, /reset, /exit")
    if startup_error is not None:
        console.print(f"{startup_error}", style="yellow")

    while True:
        try:
            line = input("mini-pi> ")
        except (EOFError, KeyboardInterrupt):
            break
        stripped = line.strip()
        if stripped in {"/exit", "/quit"}:
            break
        if stripped == "/reset":
            if agent is not None:
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
            except MiniPiError as exc:
                console.print(f"key verification failed: {exc}", style="red")
                continue
            auth_path = save_connection(new_provider, api_key, new_model)
            provider = new_provider
            model = new_model
            # 当前进程继续使用刚验证的 Key；环境变量优先级在下次启动时再生效。
            new_llm = create_llm(provider, model, api_key=api_key)
            if agent is None:
                agent = _build_agent(
                    llm=new_llm, workspace=workspace, max_steps=max_steps, renderer=renderer
                )
            else:
                agent.set_llm(new_llm)
            console.print(f"saved to {auth_path}")
            continue
        if not stripped:
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


def main() -> None:
    app()
