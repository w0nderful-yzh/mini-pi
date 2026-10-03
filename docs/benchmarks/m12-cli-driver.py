"""M12 真实 PTY/管道验收，离线模型加真实 LSP/MCP 子进程，不替换 CLI 交互。"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import select
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import ModuleType

from mini_pi.mcp.catalog import tool_alias
from mini_pi.session.jsonl import JsonlSession

PROJECT = Path(__file__).resolve().parents[2]
SECRET = "m12-environment-credential-12345"
COLOR = re.compile(r"\x1b\[(?:\d+;)*(?:3\d|4\d|9[0-7]|10[0-7])(?:;\d+)*m")


def driver() -> ModuleType:
    """复用终端创建与读取，现有旧任务脚本不参与本次模型输出。"""
    spec = importlib.util.spec_from_file_location(
        "m12_pty", Path(__file__).with_name("m7-9-4-tty-driver.py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def alive(pid: int) -> bool:
    """所有记录 PID 来自本次启动，退出后应被宿主回收。"""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def bootstrap(root: Path, script: list[dict], extra: list[str]) -> str:
    """只替换模型和测试认证路径，生产 CLI、Registry、输入、协议均原样运行。"""
    return f"""
import json
import sys
from pathlib import Path
from mini_pi.cli import app as cli
from mini_pi.llm.types import AssistantMessage, DoneEvent, TextDeltaEvent, ToolCall
from mini_pi.security import SecurityPolicy
script = {script!r}
root = Path({str(root)!r})
class Fake:
    def __init__(self):
        self.index = 0
    def stream(self, messages, tools=None):
        item = script[self.index]
        self.index += 1
        if 'calls' in item:
            yield DoneEvent(message=AssistantMessage(stop_reason='tool_calls', tool_calls=[ToolCall(id=f'call{{self.index}}-{{i}}', name=name, arguments=args) for i, (name, args) in enumerate(item['calls'])]))
        else:
            yield TextDeltaEvent(delta=item['answer'])
            yield DoneEvent(message=AssistantMessage(content=item['answer']))
    def complete(self, messages, tools=None):
        return AssistantMessage(content='summary')
original = SecurityPolicy.start_server
def start(self, server_id):
    process = original(self, server_id)
    with (root / 'pids.jsonl').open('a') as stream:
        stream.write(json.dumps({{'server': server_id, 'pid': process.pid}}) + '\\n')
    return process
SecurityPolicy.start_server = start
cli.create_llm = lambda *args, **kwargs: Fake()
cli.resolve_api_key = lambda *args, **kwargs: 'offline-test-key'
cli.load_last_connection = lambda: None
cli.save_last_connection = lambda *args: root / 'unused-auth.json'
sys.argv = ['mini-pi', '--cwd', str(root), '--no-banner', '--provider', 'openai', '--model', 'offline-test', *{extra!r}]
cli.app()
"""


def workspace(parent: Path, enabled: bool) -> tuple[Path, Path]:
    """隔离 HOME、配置、会话与文件，只配置本仓库里的离线 server。"""
    root, home = parent / "work", parent / "home"
    root.mkdir(parents=True)
    (home / ".mini-pi").mkdir(parents=True)
    (root / "app.py").write_text("from lib import target\nresult = target(2)\n")
    (root / "lib.py").write_text("def target(number: int) -> int:\n    return number + 1\n")
    servers = (
        {
            "python": {
                "kind": "lsp",
                "argv": [
                    sys.executable,
                    str(PROJECT / "tests/fixtures/lsp/server.py"),
                    "normal",
                    str(root / "lsp.jsonl"),
                ],
                "timeout": 2,
            },
            "local": {
                "kind": "mcp",
                "argv": [
                    sys.executable,
                    str(PROJECT / "tests/fixtures/mcp/server.py"),
                    "normal",
                    str(root / "mcp.jsonl"),
                ],
                "tools": ["calc.add"],
                "readonly_tools": ["calc.add"],
                "env": {"TEST_SECRET": "M12_SECRET"},
                "timeout": 2,
            },
        }
        if enabled
        else {}
    )
    (home / ".mini-pi/config.json").write_text(json.dumps({"servers": servers}))
    return root, home


def send(session: object, text: str, expected: str) -> None:
    """给 REPL 留出进入输入循环的时间，再按实际提示发送一次输入。"""
    time.sleep(0.15)
    session.send_and_wait(text, expected)


def tty(module: ModuleType, parent: Path, cols: int, no_color: bool) -> dict:
    """真实终端执行问答、双协议、拒绝/允许、取消、new、EOF 与旧会话恢复。"""
    root, home = workspace(parent, True)
    script = [
        {
            "calls": [
                (
                    "ask_user",
                    {"question": "选择 half-up 还是 bankers？", "options": ["half-up", "bankers"]},
                )
            ]
        },
        {
            "calls": [
                (tool_alias("local", "calc.add"), {"value": 2}),
                ("lsp_definition", {"path": "app.py", "line": 2, "column": 10}),
            ]
        },
        {"calls": [("bash", {"command": "printf denied > denied.txt"})]},
        {"calls": [("bash", {"command": "printf approved > approved.txt"})]},
        {"answer": "done"},
        {
            "calls": [
                (tool_alias("local", "calc.add"), {"value": 2}),
                ("lsp_definition", {"path": "app.py", "line": 2, "column": 10}),
            ]
        },
        {"answer": "new done"},
        {"calls": [("bash", {"command": "printf $$ > shell.pid; sleep 60"})]},
    ]
    session = module.PtySession(
        bootstrap(root, script, []),
        home,
        cols=cols,
        rows=24,
        env_extra={
            "NO_COLOR": "1" if no_color else "",
            "M12_SECRET": SECRET,
            "OPENAI_API_KEY": "",
            "DEEPSEEK_API_KEY": "",
        },
    )
    exited = False
    try:
        session.read_until("›")
        send(session, "task", "Approve ›")
        send(session, "1", "Answer ›")
        before = (root / "app.py").read_bytes()
        time.sleep(0.2)
        assert (root / "app.py").read_bytes() == before and not (root / "approved.txt").exists()
        send(session, "1", "Approve ›")
        send(session, "1", "Approve ›")
        send(session, "2", "Approve ›")
        send(session, "1", "done")
        session.expect_prompt()
        assert (
            not (root / "denied.txt").exists() and (root / "approved.txt").read_text() == "approved"
        )
        send(session, "/tools", "MCP ready: local")
        session.expect_prompt()
        pids = [
            item["pid"] for item in map(json.loads, (root / "pids.jsonl").read_text().splitlines())
        ]
        assert len(pids) == 2 and all(alive(pid) for pid in pids)
        old_session = next((home / ".mini-pi/sessions").rglob("*.jsonl"))
        send(session, "/new", "new session:")
        session.expect_prompt()
        assert not any(alive(pid) for pid in pids)
        send(session, "newprobe", "Approve ›")
        send(session, "1", "Approve ›")
        send(session, "1", "new done")
        session.expect_prompt()
        pids = [
            item["pid"] for item in map(json.loads, (root / "pids.jsonl").read_text().splitlines())
        ]
        assert len(pids) == 4 and all(alive(pid) for pid in pids[2:])
        send(session, "cancel", "Approve ›")
        time.sleep(0.15)
        session.buffer = ""
        os.write(session.fd, b"1\r")
        deadline = time.monotonic() + 5
        while not (root / "shell.pid").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert (root / "shell.pid").exists()
        shell_pid = int((root / "shell.pid").read_text())
        session.interrupt("Task cancelled by user.")
        session.expect_prompt()
        assert not alive(shell_pid)
        pids = [
            item["pid"] for item in map(json.loads, (root / "pids.jsonl").read_text().splitlines())
        ]
        assert len(pids) == 4 and not any(alive(pid) for pid in pids)
        new_session = next(
            path for path in (home / ".mini-pi/sessions").rglob("*.jsonl") if path != old_session
        )
        assert JsonlSession.load(new_session).replay().messages[-1].is_error
        time.sleep(0.15)
        os.write(session.fd, b"\x04")
        exited = session.wait_exit()
        assert exited
        pids = [
            item["pid"] for item in map(json.loads, (root / "pids.jsonl").read_text().splitlines())
        ]
        assert len(pids) == 4 and not any(alive(pid) for pid in pids)
        assert len(list((home / ".mini-pi/sessions").rglob("*.jsonl"))) == 2
        text = module.strip_ansi(session.output)
        assert SECRET not in text
        if no_color:
            assert not COLOR.search(session.output)
        for path in (home / ".mini-pi/sessions").rglob("*.jsonl"):
            assert SECRET not in path.read_text()
            assert JsonlSession.load(path).replay().messages
        assert "[REDACTED]" in old_session.read_text()
        durable = old_session.read_bytes()
        (home / ".mini-pi/config.json").write_text('{"servers": {}}')
        resumed = module.PtySession(
            bootstrap(root, [{"answer": "resume ok"}], ["--resume", str(old_session)]),
            home,
            cols=cols,
            rows=24,
        )
        resume_exited = False
        try:
            resumed.read_until("›")
            send(resumed, "summarize", "resume ok")
            resumed.expect_prompt()
            send(resumed, "/exit", "›")
            resume_exited = resumed.wait_exit()
            assert resume_exited and old_session.read_bytes().startswith(durable)
            assert len((root / "pids.jsonl").read_text().splitlines()) == 4
            assert JsonlSession.load(old_session).replay().messages
        finally:
            if not resume_exited:
                resumed.kill()
            os.close(resumed.fd)
        (parent / "terminal.txt").write_text(text)
        return {
            "columns": cols,
            "no_color": no_color,
            "interaction": True,
            "approval_deny_allow": True,
            "lsp_mcp": True,
            "cancel_shell_and_servers": True,
            "new": True,
            "eof": True,
            "resume": True,
            "secret_redacted": True,
            "processes_reaped": True,
        }
    finally:
        if not exited:
            if (root / "shell.pid").exists():
                try:
                    os.killpg(int((root / "shell.pid").read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if (root / "pids.jsonl").exists():
                for line in (root / "pids.jsonl").read_text().splitlines():
                    try:
                        os.killpg(json.loads(line)["pid"], signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            session.kill()
            os.waitpid(session.pid, 0)
        os.close(session.fd)


def pipe(parent: Path, mode: str) -> dict:
    """非 TTY 成功输出与必要交互错误均走真实 CLI 并验证退出码和零副作用。"""
    root, home = workspace(parent, False)
    calls = {
        "success": [("read", {"path": "app.py"})],
        "approval": [("bash", {"command": "touch forbidden.txt"})],
        "question": [("ask_user", {"question": "Choose?"})],
    }[mode]
    script = [{"calls": calls}, {"answer": "**done**"}]
    result = subprocess.run(
        [sys.executable, "-c", bootstrap(root, script, ["one-shot"])],
        cwd=PROJECT,
        env={**os.environ, "HOME": str(home), "NO_COLOR": "1", "TERM": "xterm-256color"},
        input="1\n",
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == (0 if mode == "success" else 1), result.stdout + result.stderr
    assert "\x1b[" not in result.stdout and not (root / "forbidden.txt").exists()
    messages = (
        JsonlSession.load(next((home / ".mini-pi/sessions").rglob("*.jsonl"))).replay().messages
    )
    if mode != "success":
        assert messages[-1].is_error
    else:
        assert "**done**" in result.stdout
    return {
        "mode": mode,
        "exit_code": result.returncode,
        "no_ansi": True,
        "no_unapproved_effect": True,
        "strict_replay": True,
    }


def one_shot_eof(module: ModuleType, parent: Path) -> dict:
    """一次性 TTY 在问询中收到真实 EOF 后退出 130，配对完整且不继续请求模型。"""
    root, home = workspace(parent, False)
    session = module.PtySession(
        bootstrap(root, [{"calls": [("ask_user", {"question": "Choose?"})]}], ["one-shot"]),
        home,
        cols=80,
        rows=24,
    )
    exited = False
    try:
        session.read_until("Answer ›")
        os.write(session.fd, b"\x04")
        deadline = time.monotonic() + 10
        status = None
        while time.monotonic() < deadline:
            pid, child_status = os.waitpid(session.pid, os.WNOHANG)
            if pid:
                exited, status = True, child_status
                break
            if select.select([session.fd], [], [], 0.1)[0]:
                try:
                    session._ingest(os.read(session.fd, 65536))
                except OSError:
                    pass
        assert status is not None and os.waitstatus_to_exitcode(status) == 130
        loaded = JsonlSession.load(next((home / ".mini-pi/sessions").rglob("*.jsonl")))
        assert len(loaded.replay().messages) == 4 and loaded.replay().messages[-1].is_error
        assert "Task cancelled by user." in session.output
        return {
            "mode": "one-shot-tty-EOF",
            "exit_code": 130,
            "strict_replay": True,
            "no_model_continuation": True,
        }
    finally:
        if not exited:
            session.kill()
            os.waitpid(session.pid, 0)
        os.close(session.fd)


def main() -> None:
    """执行交互、一次性 EOF 和管道场景，保存结果及本地脱敏转录。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix="mini-pi-m12-cli-")).resolve()
    module = driver()
    records = []
    for cols, no_color in ((80, False), (40, False), (80, True)):
        print(f"PTY {cols}, NO_COLOR={no_color}", flush=True)
        records.append(tty(module, root / f"tty-{cols}-{no_color}", cols, no_color))
    for mode in ("success", "approval", "question"):
        records.append(pipe(root / f"pipe-{mode}", mode))
    records.append(one_shot_eof(module, root / "one-shot-eof"))
    args.output.write_text(
        json.dumps(
            {
                "artifact_root": str(root),
                "model": "FakeLLM",
                "tty": "real PTY with scripted input",
                "cases": records,
            },
            indent=2,
        )
        + "\n"
    )
    print("seven CLI cases passed", flush=True)


if __name__ == "__main__":
    main()
