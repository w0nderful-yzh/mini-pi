"""M8.1 真实 pty 验收：紧凑启动、Markdown、/last 续读和取消清理。"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import ModuleType

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))


def _legacy_driver() -> ModuleType:
    """复用已验证的 pty 启动/终端尺寸与 FakeLLM 子进程装配。"""
    path = Path(__file__).with_name("m7-9-4-tty-driver.py")
    spec = importlib.util.spec_from_file_location("m7_9_4_tty_driver", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


LEGACY = _legacy_driver()
LEGACY.SCRIPT = [
    {"kind": "tools", "calls": [("read", {"path": "long.txt"})]},
    {"kind": "answer", "content": "**Done** with `long.txt`；中文段落。"},
    {"kind": "tools", "calls": [("bash", {"command": "sleep 60"})]},
    {"kind": "answer", "content": "done"},
]


def _workspace(root: Path) -> Path:
    """生成一个 80 行长文件，末行供单项完整展开验收。"""
    workspace = LEGACY.make_workspace(root)
    lines = [f"line-{index:02d}" for index in range(80)]
    lines[4] = "Bearer sk-secret"
    (workspace / "long.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return workspace


def _command(session: object, command: str, expected: str) -> None:
    """等提示符进入输入循环后再写 pty，避免重绘末尾抢走首个回车。"""
    time.sleep(0.25)
    try:
        session.command(command, expected)
    except AssertionError as exc:
        raise AssertionError(f"{exc}; transcript tail={session.output[-800:]!r}") from exc


def _tty(root: Path, *, cols: int, rows: int, term: str = "xterm-256color", no_color: bool = False) -> tuple[str, dict[str, bool]]:
    """真实交互终端中验证折叠、详情与低高度取消。"""
    home = root / f"home-{cols}-{rows}-{term}-{no_color}"
    home.mkdir()
    workspace = _workspace(root / f"workspace-{cols}-{rows}-{term}-{no_color}")
    session = LEGACY.PtySession(
        LEGACY.build_bootstrap(workspace, []),
        home,
        cols=cols,
        rows=rows,
        term=term,
        env_extra={"NO_COLOR": "1"} if no_color else None,
    )
    try:
        session.read_until("›")
        time.sleep(0.2)
        if term != "dumb":
            os.write(session.fd, b"\r\r\r")
            time.sleep(0.4)
        _command(session, "inspect", "Done")
        _command(session, "/last", "Last run tool calls")
        _command(session, "/last 1", "display shortened")
        _command(session, "/last 1 full", "line-79")
        if term != "dumb":
            session.send_and_wait("cancel", "Run sleep")
            time.sleep(0.3)
            session.interrupt("Task cancelled by user.")
            session.expect_prompt()
        time.sleep(0.4)
        os.write(session.fd, b"/exit\r")
        assert session.wait_exit(), f"pty did not exit: {session.output[-500:]!r}"
    finally:
        session.kill()
    text = LEGACY.strip_ansi(session.output)
    checks = {
        "compact_startup": "No Bullshit," not in text,
        "identity": "mini-pi 0.1.0" in text,
        "numbered_last": "1. ✓ Read long.txt" in text,
        "detail_hint": "/last 1 full shows captured content" in text,
        "detail_last_line": "line-79" in text,
        "secret_redacted": "sk-secret" not in text and "[REDACTED]" in text,
        "markdown_tty": "**Done**" not in text if term != "dumb" else "**Done**" in text,
        "cancelled": "Task cancelled by user." in text if term != "dumb" else True,
        "no_color_when_requested": not LEGACY.COLOR_SGR.search(session.output) if no_color or term == "dumb" else True,
    }
    return text, checks


def _pipe(root: Path) -> tuple[str, dict[str, bool]]:
    """一次性非 tty 输出保留 Markdown 原文，且无 ANSI/Live 控制。"""
    home = root / "home-pipe"
    home.mkdir()
    workspace = _workspace(root / "workspace-pipe")
    bootstrap = LEGACY.build_bootstrap(workspace, ["--no-session"]).replace(
        'sys.argv = ["mini-pi",', 'sys.argv = ["mini-pi", "inspect",'
    )
    env = {**os.environ, "HOME": str(home), "NO_COLOR": "1", "TERM": "xterm-256color"}
    result = subprocess.run(
        [sys.executable, "-c", bootstrap],
        cwd=PROJECT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    checks = {
        "completed": result.returncode == 0,
        "raw_markdown": "**Done** with `long.txt`" in result.stdout,
        "no_ansi": "\x1b[" not in result.stdout,
        "tool_line": "✓ Read long.txt" in result.stdout,
    }
    return result.stdout, checks


def main() -> int:
    """运行 40/80/120/宽屏、低高度、dumb/无色和非 tty 检查。"""
    root = Path(tempfile.mkdtemp(prefix="m8-1-tty-"))
    outputs: list[str] = []
    failed: list[str] = []
    for name, config in (
        ("40-column", {"cols": 40, "rows": 12}),
        ("80-column-low", {"cols": 80, "rows": 8}),
        ("120-column", {"cols": 120, "rows": 24}),
        ("wide-column", {"cols": 220, "rows": 24}),
        ("no-color", {"cols": 80, "rows": 12, "no_color": True}),
        ("dumb", {"cols": 80, "rows": 12, "term": "dumb"}),
    ):
        print(f"running {name}", file=sys.stderr, flush=True)
        text, checks = _tty(root, **config)
        outputs.extend((f"===== {name} =====", text, json.dumps(checks, ensure_ascii=False)))
        failed.extend(f"{name}:{key}" for key, passed in checks.items() if not passed)
    text, checks = _pipe(root)
    outputs.extend(("===== non-tty =====", text, json.dumps(checks, ensure_ascii=False)))
    failed.extend(f"non-tty:{key}" for key, passed in checks.items() if not passed)
    print("\n".join(outputs))
    print("failed checks:", ", ".join(failed) if failed else "none")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
