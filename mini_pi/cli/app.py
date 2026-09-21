"""CLI 入口：一次性任务与交互式 REPL，支持 /connect 配置凭据。"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.prompt import Prompt

from mini_pi.agent.agent import Agent
from mini_pi.auth import resolve_api_key, save_api_key
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
    provider: str = typer.Option("openai", "--provider", "-p"),
    model: str | None = typer.Option(None, "--model", "-m"),
    cwd: Path = typer.Option(Path("."), "--cwd"),
    max_steps: int = typer.Option(50, "--max-steps", min=1),
) -> None:
    workspace = Workspace(cwd)
    console = Console()
    renderer = ConsoleRenderer(console)
    agent: Agent | None = None
    startup_error: str | None = None
    try:
        agent = _build_agent(
            llm=create_llm(provider, model),
            workspace=workspace,
            max_steps=max_steps,
            renderer=renderer,
        )
    except MiniPiError as exc:
        startup_error = str(exc)

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
            try:
                verify_credentials(new_provider, api_key, model)
            except MiniPiError as exc:
                console.print(f"key verification failed: {exc}", style="red")
                continue
            auth_path = save_api_key(new_provider, api_key)
            provider = new_provider
            new_llm = create_llm(provider, model)
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


def main() -> None:
    app()
