"""M7.7c 人工 CLI 验收驱动器：真实 pty 上跑完整会话闭环并输出脱敏结果。

用法：.venv/bin/python docs/benchmarks/m7-7c-cli-driver.py <transcript_out>
不进入仓库测试集，也不联网：模型由脚本化 Fake 替换，HOME 指向临时目录，
因此不触碰真实凭据、Session 或输入历史。
"""

from __future__ import annotations

import fcntl
import json
import os
import pty
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
PYTHON = Path(sys.executable)

BOOTSTRAP = r'''
import sys
from mini_pi.cli import app as app_mod
from mini_pi.llm.types import AssistantMessage, DoneEvent, TextDeltaEvent, ToolCall

SCRIPT = __SCRIPT__
SUMMARY = __SUMMARY__

class Fake:
    def __init__(self): self.index = 0
    def stream(self, messages, tools=None):
        item = SCRIPT[min(self.index, len(SCRIPT) - 1)]
        self.index += 1
        if item["kind"] == "tool":
            call = ToolCall(id=f"c{self.index}", name="bash",
                            arguments={"command": item["command"], "timeout": 120})
            yield DoneEvent(message=AssistantMessage(stop_reason="tool_calls", tool_calls=[call]))
        else:
            yield TextDeltaEvent(delta=item["content"])
            yield DoneEvent(message=AssistantMessage(content=item["content"]))
    def complete(self, messages, tools=None):
        return AssistantMessage(content=SUMMARY)

fake = Fake()
app_mod.create_llm = lambda provider, model=None, **kwargs: fake
sys.argv = ["mini-pi"] + __ARGV__
app_mod.app()
'''

SUMMARY = (
    "## Goal\n- demo compaction in a real CLI flow\n"
    "## Progress\n- created a session and ran two large tool turns\n"
    "## Next Steps\n- continue in the compacted projection\n"
)

# 未知窗口模型：resolve_policy 返回 None，自动压缩不介入，只验证手动命令与展示
UNKNOWN_MODEL = "mini-pi-test-model"

BIG_SCRIPT = 'print("\\n".join("z" * 200 for _ in range(2000)))\n'


def build_bootstrap(script: list[dict[str, str]], argv: list[str]) -> str:
    """生成子进程启动脚本；模型与摘要都由脚本化 Fake 提供。"""
    return (
        BOOTSTRAP.replace("__SCRIPT__", repr(script))
        .replace("__SUMMARY__", repr(SUMMARY))
        .replace("__ARGV__", repr(argv))
    )


class PtySession:
    """最小 pty 会话：读写子进程终端，分别保留滑动窗口与累计输出。"""

    def __init__(self, bootstrap: str, home: Path, *, rows: int = 40, cols: int = 140) -> None:
        """fork 出子进程并按给定窗口尺寸启动。"""
        self.buffer = ""
        self.output = ""
        self.raw = b""
        pid, fd = pty.fork()
        if pid == 0:
            # 子进程在 exec 前设置 winsize，避免 Console 读取到默认 80x24
            fcntl.ioctl(1, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            os.chdir(PROJECT)
            env = dict(os.environ)
            env.update(
                HOME=str(home),
                TERM="xterm-256color",
                LANG="en_US.UTF-8",
                LC_CTYPE="en_US.UTF-8",
                PROMPT_TOOLKIT_NO_CPR="1",
            )
            env.pop("NO_COLOR", None)
            os.execve(str(PYTHON), [str(PYTHON), "-c", bootstrap], env)
        self.pid = pid
        self.fd = fd

    def _ingest(self, chunk: bytes) -> None:
        """把新输出同时写入滑动窗口与累计输出。"""
        text = chunk.decode("utf-8", errors="replace")
        self.raw += chunk
        self.buffer += text
        self.output += text

    def read_until(self, needle: str, timeout: float = 20.0) -> str:
        """持续读取直到滑动窗口出现关键字。"""
        deadline = time.monotonic() + timeout
        while needle not in self.buffer and time.monotonic() < deadline:
            ready, _, _ = select.select([self.fd], [], [], 0.2)
            if not ready:
                continue
            try:
                chunk = os.read(self.fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            self._ingest(chunk)
        assert needle in self.buffer, f"timeout waiting for {needle!r}; tail={self.buffer[-400:]!r}"
        return self.buffer

    def send(self, text: str) -> None:
        """向终端写入按键字节。"""
        os.write(self.fd, text.encode("utf-8"))

    def pump(self, timeout: float = 0.2) -> None:
        """读取一次可用输出（无数据则等待到超时），供自定义等待循环复用。"""
        ready, _, _ = select.select([self.fd], [], [], timeout)
        if ready:
            self._ingest(os.read(self.fd, 65536))

    def expect_prompt(self, timeout: float = 20.0) -> None:
        """等待下一次提示符出现；先清空滑动窗口。"""
        self.buffer = ""
        self.read_until("mini-pi>", timeout)

    def command(self, text: str, expect: str, timeout: float = 20.0) -> None:
        """输入一条命令/任务并等待预期输出，然后等待下一次提示符。"""
        self.buffer = ""
        self.send(text + "\r")
        self.read_until(expect, timeout)
        self.expect_prompt()

    def wait_exit(self, timeout: float = 15.0) -> bool:
        """等待子进程退出。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            done, _ = os.waitpid(self.pid, os.WNOHANG)
            if done:
                return True
            ready, _, _ = select.select([self.fd], [], [], 0.2)
            if ready:
                try:
                    chunk = os.read(self.fd, 65536)
                except OSError:
                    chunk = b""
                if chunk:
                    self._ingest(chunk)
            time.sleep(0.05)
        return False

    def kill(self) -> None:
        """兜底清理子进程。"""
        try:
            os.kill(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def exit_repl(self) -> None:
        """发送 /exit 并等待进程结束；交互模式不像一次性模式那样打印 Session 路径。"""
        self.buffer = ""
        self.send("/exit\r")
        assert self.wait_exit(), "REPL did not exit after /exit"


def session_files(home: Path) -> list[Path]:
    """列出临时 HOME 下的 JSONL 会话文件。"""
    root = home / ".mini-pi" / "sessions"
    return sorted(root.rglob("*.jsonl")) if root.exists() else []


def phase_create(home: Path, workdir: Path) -> dict[str, object]:
    """阶段 1：新建会话、两次真实工具调用、/status /context /sessions、退出。"""
    script = [
        {"kind": "tool", "command": "python3 big.py"},
        {"kind": "tool", "command": "python3 big.py"},
        {"kind": "answer", "content": "created session"},
    ]
    # 未知窗口模型：关闭自动压缩，让本次人工验收只观察手动 /compact 路径
    argv = ["--cwd", str(workdir), "--no-banner", "--model", UNKNOWN_MODEL]
    pty_session = PtySession(build_bootstrap(script, argv), home)
    try:
        pty_session.read_until("mini-pi>")
        pty_session.command("run the big script twice then reply done", "created session")
        # 语义化工具事件：确定性标题 + 真实退出码 + 截断标记
        assert "Run python3" in pty_session.output, pty_session.output[-800:]
        assert "shell exited 0" in pty_session.output
        assert "output truncated" in pty_session.output
        pty_session.command("/status", "Current context")
        pty_session.command("/status full", "Session path:")
        pty_session.command("/context", "Tool results:")
        pty_session.command("/sessions", "Saved sessions for")
        assert "* current session" in pty_session.output
        pty_session.exit_repl()
    finally:
        pty_session.kill()
    files = session_files(home)
    assert len(files) == 1, files
    entries = [json.loads(line) for line in files[0].read_text().splitlines()]
    roles = [
        entry["message"]["role"]
        for entry in entries
        if entry.get("type") == "message"
    ]
    assert roles[0] == "system" and roles[-1] == "assistant"
    assert "tool" in roles
    return {"session": str(files[0]), "message_roles": roles, "entry_ids": [entry["id"] for entry in entries]}


def phase_resume(home: Path, workdir: Path, session_path: Path, phase1: dict[str, object]) -> dict[str, object]:
    """阶段 2：恢复 → /compact → 继续 → /new，并复核 append-only。"""
    script = [
        {"kind": "answer", "content": "continued after compact"},
        {"kind": "answer", "content": "answer in new session"},
    ]
    argv = ["--cwd", str(workdir), "--resume", str(session_path), "--no-banner"]
    pty_session = PtySession(build_bootstrap(script, argv), home)
    try:
        pty_session.read_until("mini-pi>")
        pty_session.command("/compact", "Compaction: summarized")
        assert "Summary usage:" in pty_session.output
        pty_session.command("/context", "Summaries:")
        pty_session.command("continue after compact", "continued after compact")
        pty_session.command("/status", "Last run requests")
        pty_session.command("/new", "new session:")
        pty_session.command("task in new session", "answer in new session")
        pty_session.exit_repl()
    finally:
        pty_session.kill()

    original = [
        json.loads(line) for line in session_path.read_text().splitlines()
    ]
    ids_now = {entry["id"] for entry in original}
    previous_ids = set(phase1["entry_ids"])  # type: ignore[arg-type]
    assert previous_ids <= ids_now, "append-only violated: earlier entries disappeared"
    assert any(entry.get("type") == "compaction" for entry in original)
    summary_entry = next(entry for entry in original if entry.get("type") == "compaction")
    files_after = session_files(home)
    assert len(files_after) == 2, files_after  # /new 产生独立会话
    return {
        "compaction_entry": {
            "tokensBefore": summary_entry["tokensBefore"],
            "firstKeptEntryId": summary_entry["firstKeptEntryId"],
            "summaryChars": len(summary_entry["summary"]),
        },
        "session_count": len(files_after),
        "append_only": True,
    }


def phase_budget(home: Path, workdir: Path) -> dict[str, object]:
    """阶段 3：小预算下的请求边界提示（警告或安全停止）。"""
    script = [
        {"kind": "tool", "command": "python3 big.py"},
        {"kind": "answer", "content": "finished under budget"},
    ]
    argv = [
        "--cwd", str(workdir), "--no-banner",
        "--model", UNKNOWN_MODEL, "--max-run-input-tokens", "5000",
    ]
    pty_session = PtySession(build_bootstrap(script, argv), home)
    try:
        pty_session.read_until("mini-pi>")
        pty_session.buffer = ""
        pty_session.send("run a tiny command\r")
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if (
                "Run input budget is close" in pty_session.buffer
                or "input budget would be exceeded" in pty_session.buffer
            ):
                break
            pty_session.pump()
        output = pty_session.output
        if "Run input budget is close" in output:
            result: dict[str, object] = {"kind": "warning"}
        elif "input budget would be exceeded" in output:
            result = {"kind": "budget_limit"}
        else:
            raise AssertionError(f"no budget notice observed; tail={output[-600:]!r}")
        result["tool_event"] = "Run python3" in output
        # 预算停止后先回到提示符，再发送 /exit，避免按键落进行规程缓冲
        pty_session.expect_prompt()
        pty_session.exit_repl()
    finally:
        pty_session.kill()
    return result


def phase_narrow_interrupt(home: Path, workdir: Path) -> dict[str, object]:
    """阶段 4：窄屏降级、输入历史与连续两次 Ctrl+C。"""
    script = [{"kind": "answer", "content": "narrow ok"}]
    argv = ["--cwd", str(workdir), "--model", UNKNOWN_MODEL]
    pty_session = PtySession(build_bootstrap(script, argv), home, rows=24, cols=40)
    try:
        pty_session.read_until("mini-pi>")
        startup = pty_session.output
        # 窄屏：Banner 降级为单行，启动元数据按字段分行
        assert "牛人，就用牛的 coding agent" in startup, startup
        assert "model openai/" in startup and "project " in startup
        pty_session.command("narrow task", "narrow ok")
        pty_session.send("\x03")
        pty_session.read_until("Press Ctrl+C again to exit.")
        pty_session.send("\x03")
        pty_session.read_until("exiting mini-pi.")
        assert pty_session.wait_exit()
    finally:
        pty_session.kill()
    history = home / ".mini-pi" / "history"
    assert history.is_file(), history
    content = history.read_text(encoding="utf-8")
    assert "+narrow task" in content, content
    return {"banner_fallback": True, "history_has_task": True, "interrupt_exit": True}


def phase_non_tty(workdir: Path) -> dict[str, object]:
    """阶段 5：非 tty 管道输入仍可用，且不写输入历史。"""
    non_tty_home = workdir.parent / "home-nontty"
    non_tty_home.mkdir(exist_ok=True)
    bootstrap = build_bootstrap([{"kind": "answer", "content": "unused"}], ["--cwd", str(workdir)])
    env = dict(os.environ)
    env.update(
        HOME=str(non_tty_home), TERM="xterm-256color", LANG="en_US.UTF-8", LC_CTYPE="en_US.UTF-8"
    )
    env.pop("NO_COLOR", None)
    completed = subprocess.run(
        [str(PYTHON), "-c", bootstrap],
        cwd=str(PROJECT),
        input="/help\n/exit\n",
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr[-600:]
    assert "Input: Enter submits" in completed.stdout
    assert not (non_tty_home / ".mini-pi" / "history").exists(), "non-tty must not create history"
    return {"exit_code": completed.returncode, "plain_input_fallback": True, "history_created": False}


def main() -> int:
    """按阶段跑完整闭环并写出脱敏结果。"""
    out_path = Path(sys.argv[1])
    root = Path(tempfile.mkdtemp(prefix="m7-7c-"))
    home = root / "home"
    workdir = root / "work"
    home.mkdir()
    workdir.mkdir()
    (workdir / "big.py").write_text(BIG_SCRIPT, encoding="utf-8")

    report: dict[str, object] = {}
    phase1 = phase_create(home, workdir)
    report["phase1_create"] = phase1
    report["phase2_resume"] = phase_resume(home, workdir, Path(str(phase1["session"])), phase1)
    report["phase3_budget"] = phase_budget(home, workdir)
    report["phase4_narrow_interrupt"] = phase_narrow_interrupt(home, workdir)
    report["phase5_non_tty"] = phase_non_tty(workdir)

    payload = json.dumps(report, ensure_ascii=False, indent=2)
    out_path.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
