"""M7.9.4 视觉验收驱动器：在真实 pty 上驱动 mini-pi 并保存脱敏转录。

用法（在仓库根执行）：uv run python docs/benchmarks/m7-9-4-tty-driver.py <out_dir>

覆盖：80 列完整任务流（成功 / shell 非零 / 编辑改动 / 收尾统计 / 状态页）、40 列窄屏
降级、TERM=dumb、NO_COLOR、非 tty 一次性输出、任务中 Ctrl+C。模型由脚本化 Fake 提供，
HOME 指向临时目录，全程不联网、不触碰真实凭据、Session 与输入历史。
"""

from __future__ import annotations

import fcntl
import json
import os
import pty
import re
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
ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b[()][A-Z0-9]|\x1b[=>]")
# 只匹配 SGR 颜色码：TERM=dumb / NO_COLOR 下我们不应再输出前景色
COLOR_SGR = re.compile(r"\x1b\[(?:[0-9]+;)*3[1-6]m")

APP_SOURCE = '''def add(a: int, b: int) -> int:
    return a - b
'''
TEST_SOURCE = '''from app import add


def test_add() -> None:
    assert add(2, 3) == 5
'''

# FakeLLM 的回复脚本：每项要么请求工具，要么给出最终回答
SCRIPT = [
    {"kind": "tools", "calls": [("read", {"path": "app.py"})]},
    {"kind": "tools", "calls": [("bash", {"command": "grep -n missing app.py"})]},
    {
        "kind": "tools",
        "calls": [
            (
                "edit",
                {
                    "path": "app.py",
                    "edits": [{"old_text": "return a - b", "new_text": "return a + b"}],
                },
            )
        ],
    },
    {"kind": "tools", "calls": [("bash", {"command": "__PYTEST__ -q tests"})]},
    {"kind": "answer", "content": "add 现在返回 a + b，tests 通过。"},
    {"kind": "tools", "calls": [("bash", {"command": "sleep 60"})]},
    {"kind": "answer", "content": "done"},
]

BOOTSTRAP = r'''
import sys
from mini_pi.cli import app as app_mod
from mini_pi.llm.types import AssistantMessage, DoneEvent, TextDeltaEvent, ToolCall

SCRIPT = __SCRIPT__

class Fake:
    def __init__(self):
        self.index = 0
    def stream(self, messages, tools=None):
        item = SCRIPT[min(self.index, len(SCRIPT) - 1)]
        self.index += 1
        if item["kind"] == "tools":
            calls = [
                ToolCall(id=f"c{self.index}-{i}", name=name, arguments=dict(arguments))
                for i, (name, arguments) in enumerate(item["calls"])
            ]
            yield DoneEvent(message=AssistantMessage(stop_reason="tool_calls", tool_calls=calls))
        else:
            yield TextDeltaEvent(delta=item["content"])
            yield DoneEvent(message=AssistantMessage(content=item["content"]))
    def complete(self, messages, tools=None):
        return AssistantMessage(content="ok")

app_mod.create_llm = lambda provider, model=None, **kwargs: Fake()
app_mod.save_last_connection = lambda *args, **kwargs: None
sys.argv = ["mini-pi", "--cwd", "__WORKDIR__", "--provider", "openai",
            "--model", "gpt-5.6-terra", *__EXTRA__]
app_mod.app()
'''


def build_bootstrap(workdir: Path, extra: list[str]) -> str:
    """生成子进程启动脚本；工具与脚本化模型都在子进程内装配。"""
    script = json.loads(json.dumps(SCRIPT).replace("__PYTEST__", f"{PYTHON} -m pytest"))
    return (
        BOOTSTRAP.replace("__SCRIPT__", repr(script))
        .replace("__WORKDIR__", str(workdir))
        .replace("__EXTRA__", repr(extra))
    )


class PtySession:
    """最小 pty 会话：按给定窗口尺寸启动并累计输出。"""

    def __init__(
        self,
        bootstrap: str,
        home: Path,
        *,
        rows: int = 40,
        cols: int = 80,
        term: str = "xterm-256color",
        env_extra: dict[str, str] | None = None,
    ) -> None:
        """fork 出子进程并设置 winsize 与环境；默认不继承 NO_COLOR。"""
        self.buffer = ""
        self.output = ""
        pid, fd = pty.fork()
        if pid == 0:
            fcntl.ioctl(1, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            os.chdir(PROJECT)
            env = dict(os.environ)
            env.update(
                HOME=str(home),
                TERM=term,
                LANG="en_US.UTF-8",
                LC_CTYPE="en_US.UTF-8",
                PROMPT_TOOLKIT_NO_CPR="1",  # 伪终端不回应 CPR 查询
            )
            env.pop("NO_COLOR", None)
            env.update(env_extra or {})
            os.execve(str(PYTHON), [str(PYTHON), "-c", bootstrap], env)
        self.pid = pid
        self.fd = fd

    def _ingest(self, chunk: bytes) -> None:
        """把新输出同时写入滑动窗口与累计输出。"""
        text = chunk.decode("utf-8", errors="replace")
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

    def expect_prompt(self, timeout: float = 20.0) -> None:
        """等待下一次提示符；先清空滑动窗口避免匹配历史画面。"""
        self.buffer = ""
        self.read_until("›", timeout)

    def send_and_wait(self, text: str, expect: str, timeout: float = 20.0) -> None:
        """输入一条命令或任务并等待预期输出；不等待提示符。"""
        self.buffer = ""
        os.write(self.fd, (text + "\r").encode("utf-8"))
        self.read_until(expect, timeout)

    def command(self, text: str, expect: str, timeout: float = 20.0) -> None:
        """输入一条命令或任务，等待预期输出后回到提示符。"""
        self.send_and_wait(text, expect, timeout)
        self.expect_prompt()

    def interrupt(self, expect: str, timeout: float = 20.0) -> None:
        """发送 Ctrl+C 并等待预期输出。"""
        self.buffer = ""
        os.write(self.fd, b"\x03")
        self.read_until(expect, timeout)

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
                    self._ingest(os.read(self.fd, 65536))
                except OSError:
                    pass
            time.sleep(0.05)
        return False

    def kill(self) -> None:
        """兜底清理子进程。"""
        try:
            os.kill(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def make_workspace(root: Path) -> Path:
    """建立带一个失败用例的临时工作区；重复调用会重置文件内容。"""
    workdir = root / "work"
    (workdir / "tests").mkdir(parents=True, exist_ok=True)
    (workdir / "app.py").write_text(APP_SOURCE, encoding="utf-8")
    (workdir / "tests/test_app.py").write_text(TEST_SOURCE, encoding="utf-8")
    return workdir


FRAME_CHARS = "◜◝◞◟"


def _collapse_activity(line: str) -> str:
    """同一行里反复重绘的活动帧只保留最后一次；单帧行原样保留。"""
    frames = [index for index, char in enumerate(line) if char in FRAME_CHARS]
    if len(frames) < 2:
        return line
    return "⟨activity⟩ " + line[frames[-1] + 1 :]


def strip_ansi(text: str) -> str:
    """去掉 ANSI 控制序列并折叠反复重绘的活动区，便于阅读转录。"""
    cleaned = ANSI.sub("", text).replace("\r", "")
    return "\n".join(_collapse_activity(line.rstrip()) for line in cleaned.split("\n"))


def flow_80(root: Path) -> tuple[str, dict[str, object]]:
    """80 列：完整任务流 + 状态页 + 任务中取消。"""
    home = root / "home80"
    home.mkdir()
    workdir = make_workspace(root)
    session = PtySession(build_bootstrap(workdir, []), home, cols=80)
    checks: dict[str, object] = {}
    try:
        session.read_until("›")
        session.command("inspect and fix the failing test", "/last for the")
        session.command("/last", "Last run tool calls")
        session.command("/status", "Compaction")
        session.command("/context", "cost-aware early compaction")
        session.command("/tools", "git_status")
        session.command("/sessions", "Saved sessions for")
        # 长任务不等待提示符：工具还在运行时直接 Ctrl+C
        session.send_and_wait("run a long command", "Run sleep")
        # 等工具真正开始执行再中断：start 事件渲染期间的中断属于另一条路径
        time.sleep(0.6)
        session.interrupt("Task cancelled by user.")
        session.expect_prompt()
        os.write(session.fd, b"/exit\r")
        session.wait_exit()
    finally:
        session.kill()
    text = strip_ansi(session.output)
    checks["startup_identity_bar"] = "mini-pi 0.1.0 · openai/gpt-5.6-terra" in text
    checks["help_right_aligned"] = bool(re.search(r"session \w+\s+/help for commands", text))
    # tty 默认折叠：成功的工具调用不占滚动区，失败与折叠入口必须可见
    # 唯一一次出现来自 /last 的展开，滚动区里没有它
    checks["success_calls_collapsed"] = text.count("✓ Read app.py · completed") == 1
    checks["shell_nonzero_marker"] = "✗ Run grep · shell exited 1" in text
    checks["collapse_hint"] = "/last for the 3 collapsed tool calls" in text
    checks["last_run_expands"] = "Last run tool calls (4):" in text
    checks["last_run_keeps_files"] = "1 file(s) changed" in text
    checks["summary_line"] = "Completed · 4 tools · 5 requests" in text
    checks["status_aligned"] = bool(re.search(r"^Session\s{9}\w{8}$", text, re.M))
    checks["cancel_line"] = "✗ Task cancelled by user." in text
    checks["cancel_summary"] = "\nCancelled · 1 tools · 1 requests" in text
    checks["activity_line_rendered"] = text.count("⟨activity⟩") >= 3
    checks["old_art_removed"] = "db         db" not in text
    return text, checks


def flow_40(root: Path) -> tuple[str, dict[str, object]]:
    """40 列：启动降级 + 单轮工具流程不越界。"""
    home = root / "home40"
    home.mkdir()
    workdir = make_workspace(root)
    session = PtySession(build_bootstrap(workdir, ["--no-banner"]), home, cols=40)
    try:
        session.read_until("›")
        session.command("inspect the failing test", "tests 通过。")
        os.write(session.fd, b"/exit\r")
        session.wait_exit()
    finally:
        session.kill()
    text = strip_ansi(session.output)
    lines = [line for line in text.split("\n") if line.strip()]
    checks: dict[str, object] = {
        "startup_field_lines": "model openai/gpt-5.6-terra" in text,
        "help_on_own_line": "/help for commands" in text,
        "identity_bar_absent": "mini-pi 0.1.0 · openai/gpt-5.6-terra" not in text,
        "collapse_hint": "/last for the" in text,
        "success_calls_collapsed": "✓ Read app.py · completed" not in text,
        "no_line_over_40": all(len(line) <= 40 for line in lines),
    }
    return text, checks


def flow_dumb_and_nocolor(root: Path) -> tuple[str, dict[str, object]]:
    """TERM=dumb 走字段行；NO_COLOR 下仍能读出全部文本状态。"""
    home = root / "home-dumb"
    home.mkdir()
    workdir = make_workspace(root)
    dumb = PtySession(
        build_bootstrap(workdir, ["--no-banner"]), home, cols=80, term="dumb"
    )
    try:
        dumb.read_until("›")
        os.write(dumb.fd, b"/exit\r")
        dumb.wait_exit()
    finally:
        dumb.kill()
    dumb_text = strip_ansi(dumb.output)

    home_nc = root / "home-nc"
    home_nc.mkdir()
    no_color = PtySession(
        build_bootstrap(workdir, ["--no-banner"]),
        home_nc,
        cols=80,
        term="xterm-256color",
        env_extra={"NO_COLOR": "1"},
    )
    try:
        no_color.read_until("›")
        # --no-banner 只关启动 Art：tty 内的活动区与折叠仍应生效
        no_color.command("inspect the failing test", "/last for the")
        os.write(no_color.fd, b"/exit\r")
        no_color.wait_exit()
    finally:
        no_color.kill()
    nocolor_text = strip_ansi(no_color.output)

    checks: dict[str, object] = {
        "dumb_field_lines": "model openai/gpt-5.6-terra" in dumb_text,
        "dumb_no_color": not COLOR_SGR.search(dumb.output),
        "no_banner_suppresses_art": "No Bullshit," not in nocolor_text,
        "no_color_still_readable": "mini-pi 0.1.0" in nocolor_text,
        "no_color_no_color": not COLOR_SGR.search(no_color.output),
        "no_banner_keeps_activity": "⟨activity⟩" in nocolor_text,
        "no_color_collapses": "/last for the" in nocolor_text,
    }
    return dumb_text + "\n--- NO_COLOR + --no-banner ---\n" + nocolor_text, checks


def flow_pipe(root: Path) -> tuple[str, dict[str, object]]:
    """非 tty：一次性任务输出确定性文本，无 ANSI、无 Live 区域。"""
    home = root / "home-pipe"
    home.mkdir()
    workdir = make_workspace(root)
    bootstrap = build_bootstrap(workdir, ["--no-session"]).replace(
        'sys.argv = ["mini-pi",', 'sys.argv = ["mini-pi", "inspect and fix the failing test",'
    )
    env = dict(os.environ)
    env.update(HOME=str(home), TERM="xterm-256color", NO_COLOR="1")
    result = subprocess.run(
        [str(PYTHON), "-c", bootstrap],
        cwd=PROJECT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    text = strip_ansi(result.stdout)
    checks: dict[str, object] = {
        "exit_code_zero": result.returncode == 0,
        "no_ansi": "\x1b[" not in result.stdout,
        "tool_lines": "✓ Read app.py · completed" in text,
        "summary_line": "Completed ·" in text,
        "old_art_removed": "db         db" not in text,
    }
    return text, checks


def main() -> int:
    """依次跑四种终端形态，输出转录与断言结果。"""
    out_dir = Path(sys.argv[1])
    out_dir.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="m7-9-4-tty-"))

    sections: list[tuple[str, str, dict[str, object]]] = []
    for name, runner in (
        ("80-column task flow", flow_80),
        ("40-column narrow", flow_40),
        ("TERM=dumb and NO_COLOR", flow_dumb_and_nocolor),
        ("non-tty one-shot", flow_pipe),
    ):
        text, checks = runner(root)
        sections.append((name, text, checks))
        (out_dir / f"{name.split()[0].replace('=', '-').lower()}.txt").write_text(
            text, encoding="utf-8"
        )

    failed = [key for _, _, checks in sections for key, value in checks.items() if not value]
    transcript = []
    for name, text, checks in sections:
        transcript.append(f"===== {name} =====")
        transcript.append(text.rstrip())
        transcript.append("--- checks ---")
        transcript.append(json.dumps(checks, ensure_ascii=False, indent=2))
    transcript.append("===== summary =====")
    transcript.append("failed checks: " + (", ".join(failed) if failed else "none"))
    (out_dir / "m7-9-4-cli-visual.txt").write_text("\n\n".join(transcript) + "\n", encoding="utf-8")
    print("\n\n".join(transcript))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
