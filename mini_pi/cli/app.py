"""CLI 入口：一次性任务与交互式 REPL。"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from mini_pi.agent.agent import Agent
from mini_pi.cli.console import ConsoleRenderer
from mini_pi.errors import MiniPiError
from mini_pi.llm.base import LLMClient
from mini_pi.llm.deepseek_client import DeepSeekClient
from mini_pi.llm.openai_client import OpenAIClient
from mini_pi.tools import build_default_registry
from mini_pi.workspace.workspace import Workspace

app = typer.Typer(add_completion=False, help="mini-pi: a lightweight Python coding agent")


def create_llm(provider: str, model: str | None) -> LLMClient:
    """按 provider 构造客户端；model 缺省用各自的默认模型。"""
    if provider == "openai":
        return OpenAIClient(model=model or "gpt-4o-mini")
    if provider == "deepseek":
        return DeepSeekClient(model=model or "deepseek-chat")
    raise typer.BadParameter(f"unsupported provider: {provider!r} (expected 'openai' or 'deepseek')")


@app.command()
def cli(
    prompt: str | None = typer.Argument(None, help="Task to run once; omit to start an interactive session."),
    provider: str = typer.Option("openai", "--provider", "-p"),
    model: str | None = typer.Option(None, "--model", "-m"),
    cwd: Path = typer.Option(Path("."), "--cwd"),
    max_steps: int = typer.Option(50, "--max-steps", min=1),
) -> None:
    try:
        llm = create_llm(provider, model)
    except MiniPiError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    workspace = Workspace(cwd)
    renderer = ConsoleRenderer()
    agent = Agent(
        llm=llm,
        registry=build_default_registry(workspace),
        cwd=workspace.root,
        max_steps=max_steps,
        on_event=renderer.handle,
    )
    if prompt is not None:
        agent.run(prompt)
        return

    console = Console()
    console.print("mini-pi interactive mode. Commands: /reset, /exit")
    while True:
        try:
            line = input("mini-pi> ")
        except (EOFError, KeyboardInterrupt):
            break
        stripped = line.strip()
        if stripped in {"/exit", "/quit"}:
            break
        if stripped == "/reset":
            agent.reset()
            console.print("context cleared")
            continue
        if not stripped:
            continue
        try:
            agent.run(stripped)
        except KeyboardInterrupt:
            # 只中断当前任务，不退出交互
            console.print("interrupted", style="yellow")


def main() -> None:
    app()
