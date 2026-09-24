"""M7.D2 tty 人工记录驱动器：在真实 pty 上驱动 mini-pi REPL 并保存原始转录。

用法：.venv/bin/python /tmp/m7d2_tty_check.py <transcript_out>
不进入仓库；只在验收时手动运行，隔离 HOME，不触碰真实凭据与 Session。
"""

from __future__ import annotations

import json
import os
import pty
import select
import signal
import sys
import tempfile
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
PYTHON = Path(sys.executable)

BOOTSTRAP = r'''
import sys
from mini_pi.cli import app as app_mod
from mini_pi.llm.types import AssistantMessage, DoneEvent, TextDeltaEvent, ToolCall

CALLS = []
SCRIPT = [
    {"kind": "answer", "content": "acknowledged"},
    {"kind": "tool", "command": "echo $$ > __PIDFILE__; sleep 60"},
]

class Fake:
    def __init__(self): self.index = 0
    def stream(self, messages, tools=None):
        CALLS.append([m.content for m in messages if getattr(m, "role", "") == "user"])
        __WRITE_CALLS__
        item = SCRIPT[min(self.index, len(SCRIPT) - 1)]
        self.index += 1
        if item["kind"] == "tool":
            call = ToolCall(id="c1", name="bash", arguments={"command": item["command"], "timeout": 120})
            yield DoneEvent(message=AssistantMessage(stop_reason="tool_calls", tool_calls=[call]))
        else:
            yield TextDeltaEvent(delta=item["content"])
            yield DoneEvent(message=AssistantMessage(content=item["content"]))
    def complete(self, messages, tools=None):
        return AssistantMessage(content="ok")

fake = Fake()
app_mod.create_llm = lambda provider, model=None, **kwargs: fake
app_mod.save_last_connection = lambda *args, **kwargs: None
sys.argv = ["mini-pi", "--cwd", "__WORKDIR__", "--no-banner", "--provider", "openai", "--model", "gpt-5.6-terra"]
app_mod.app()
'''


def build_bootstrap(workdir: Path, pidfile: Path, callsfile: Path) -> str:
    """生成子进程启动脚本，替换工作目录、PID 标记与调用转储路径。"""
    write_calls = (
        "import json as _json; "
        f"open({str(callsfile)!r}, 'w').write(_json.dumps(CALLS))"
    )
    return (
        BOOTSTRAP.replace("__WRITE_CALLS__", write_calls)
        .replace("__PIDFILE__", str(pidfile))
        .replace("__WORKDIR__", str(workdir))
    )


class Session:
    """最小 pty 会话：读写子进程终端并等待关键字出现。"""

    def __init__(self, bootstrap: str, home: Path) -> None:
        """fork 出子进程并记录缓冲区。"""
        self.buffer = ""
        self.raw = b""
        pid, fd = pty.fork()
        if pid == 0:
            os.chdir(PROJECT)
            env = dict(os.environ)
            env.update(
                HOME=str(home),
                TERM="xterm-256color",
                LANG="en_US.UTF-8",
                LC_CTYPE="en_US.UTF-8",
                # 伪终端不回应 CPR 查询；关闭探测以免按键被探测等待吞掉
                PROMPT_TOOLKIT_NO_CPR="1",
            )
            env.pop("NO_COLOR", None)
            os.execve(str(PYTHON), [str(PYTHON), "-c", bootstrap], env)
        self.pid = pid
        self.fd = fd

    def read_until(self, needle: str, timeout: float = 15.0) -> str:
        """持续读取直到出现关键字；超时抛出断言便于定位。"""
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
            self.raw += chunk
            self.buffer += chunk.decode("utf-8", errors="replace")
        assert needle in self.buffer, f"timeout waiting for {needle!r}; tail={self.buffer[-500:]!r}"
        return self.buffer

    def send(self, text: str) -> None:
        """向终端写入按键字节。"""
        os.write(self.fd, text.encode("utf-8"))

    def clear(self) -> None:
        """丢弃已消费的输出，便于等待下一个真实提示符。"""
        self.buffer = ""

    def expect_prompt(self, timeout: float = 15.0) -> None:
        """等待下一次提示符出现；先清空旧输出避免匹配到历史画面。"""
        self.clear()
        self.read_until("mini-pi>", timeout)

    def wait_exit(self, timeout: float = 10.0) -> bool:
        """等待子进程退出；返回是否在超时前结束。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            done, status = os.waitpid(self.pid, os.WNOHANG)
            if done:
                return True
            ready, _, _ = select.select([self.fd], [], [], 0.2)
            if ready:
                try:
                    chunk = os.read(self.fd, 65536)
                except OSError:
                    chunk = b""
                if chunk:
                    self.raw += chunk
                    self.buffer += chunk.decode("utf-8", errors="replace")
            time.sleep(0.05)
        return False

    def kill(self) -> None:
        """兜底清理子进程组。"""
        try:
            os.kill(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def main() -> int:
    """执行完整 tty 场景并写出转录。"""
    out_path = Path(sys.argv[1])
    tmp_root = Path(tempfile.mkdtemp(prefix="m7d2-tty-"))
    home = tmp_root / "home"
    workdir = tmp_root / "work"
    home.mkdir()
    workdir.mkdir()
    pidfile = workdir / "child.pid"
    callsfile = tmp_root / "calls.json"

    session = Session(build_bootstrap(workdir, pidfile, callsfile), home)
    transcript: list[str] = []
    try:
        session.read_until("mini-pi>")
        transcript.append("=== startup prompt ===")

        # 1) 多行输入：Ctrl+J 换行后再 Enter 提交，只应产生一条 user 消息
        session.send("/help\r")
        session.read_until("Ctrl+C cancels the running task")
        transcript.append("=== /help 显示输入键位 ===")
        session.expect_prompt()

        session.send("line one\x0aline two\r")
        session.read_until("acknowledged")
        transcript.append("=== 多行提交 ===")
        session.expect_prompt()

        # 2) 空闲时第一次 Ctrl+C 只提示，随后成功提交文本会把计数清零
        session.send("\x03")
        session.read_until("Press Ctrl+C again to exit.")
        transcript.append("=== 第一次空闲 Ctrl+C ===")
        session.expect_prompt()

        # 3) 任务中的 Ctrl+C：bash 进程组必须被杀，且以 cancelled 结束
        session.send("run the sleep command\r")
        session.read_until("Run compound shell command")
        deadline = time.monotonic() + 10
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert pidfile.exists(), "bash child pid file was not created"
        child_pid = int(pidfile.read_text().strip())
        time.sleep(0.5)
        session.send("\x03")
        session.read_until("Task cancelled by user.")
        transcript.append("=== 任务中 Ctrl+C ===")

        # 4) 取消后紧接着的空闲 Ctrl+C 命中“连续两次”语义，直接退出
        session.expect_prompt()
        session.send("\x03")
        session.read_until("exiting mini-pi.")
        transcript.append("=== 取消后第二次 Ctrl+C 退出 ===")
        assert session.wait_exit(), "child did not exit after two interrupts"

        # 4) 进程组清理与事实保留检查
        alive = True
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                alive = False
                break
            time.sleep(0.05)
        assert not alive, f"bash process group {child_pid} survived Ctrl+C"

        history = (home / ".mini-pi" / "history").read_text(encoding="utf-8")
        calls = json.loads(callsfile.read_text(encoding="utf-8"))
        sessions = sorted((home / ".mini-pi" / "sessions").rglob("*.jsonl"))
        entries = [json.loads(line) for path in sessions for line in path.read_text().splitlines()]

        report = {
            "child_pid": child_pid,
            "child_alive_after_cancel": alive,
            "history_contains_multiline": "+line one\n+line two" in history,
            "llm_user_messages": calls,
            "session_entries": [
                {
                    "type": entry.get("type"),
                    "role": (entry.get("message") or {}).get("role"),
                    "content": (entry.get("message") or {}).get("content"),
                    "is_error": (entry.get("message") or {}).get("is_error"),
                    "tool_call_id": (entry.get("message") or {}).get("tool_call_id"),
                }
                for entry in entries
            ],
        }
    finally:
        session.kill()

    transcript.append("=== 校验结果 ===")
    transcript.append(json.dumps(report, ensure_ascii=False, indent=2))
    out_path.write_text("\n\n".join(transcript) + "\n", encoding="utf-8")
    print("\n\n".join(transcript))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
