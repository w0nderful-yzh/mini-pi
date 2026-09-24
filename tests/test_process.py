from __future__ import annotations

import _thread
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from mini_pi.tools.process import run_process, run_shell


def test_run_process_captures_stdout(tmp_path: Path) -> None:
    """argv 形式执行，stdout/stderr 分离捕获。"""
    result = run_process([sys.executable, "-c", "print('hello')"], cwd=tmp_path, timeout_s=30)
    assert result.exit_code == 0
    assert result.stdout.strip() == "hello"
    assert result.stderr == ""
    assert result.timed_out is False


def test_run_process_captures_stderr_and_exit_code(tmp_path: Path) -> None:
    """非 0 退出码与 stderr 原样返回，不抛异常。"""
    code = "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"
    result = run_process([sys.executable, "-c", code], cwd=tmp_path, timeout_s=30)
    assert result.exit_code == 3
    assert result.stdout.strip() == "out"
    assert result.stderr.strip() == "err"


def test_run_shell_executes_pipeline(tmp_path: Path) -> None:
    """shell 模式支持管道等 shell 语法。"""
    result = run_shell("echo hi | tr a-z A-Z", cwd=tmp_path, timeout_s=30)
    assert result.exit_code == 0
    assert result.stdout.strip() == "HI"


def test_timeout_kills_process_group(tmp_path: Path) -> None:
    """超时必须杀掉整个进程组，避免子进程残留。"""
    started = time.monotonic()
    result = run_process(
        [sys.executable, "-c", "import time; time.sleep(30)"], cwd=tmp_path, timeout_s=1
    )
    elapsed = time.monotonic() - started
    assert result.timed_out is True
    assert elapsed < 10


def test_large_output_is_capped_and_flagged(tmp_path: Path) -> None:
    """超大输出被截断到内存上限，并标记 truncated。"""
    code = "print('x' * 3_000_000)"
    result = run_process(
        [sys.executable, "-c", code], cwd=tmp_path, timeout_s=30, max_output_bytes=10_000
    )
    assert result.exit_code == 0
    assert result.stdout_truncated is True
    assert len(result.stdout) <= 10_000


def test_tail_keep_keeps_last_bytes(tmp_path: Path) -> None:
    """keep=tail 保留结尾输出（报错通常出现在后面）。"""
    code = "import sys; sys.stdout.write('A' * 5000); sys.stdout.write('Z' * 5000)"
    result = run_process(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        timeout_s=30,
        max_output_bytes=1000,
        keep="tail",
    )
    assert result.stdout == "Z" * 1000


def test_head_keep_keeps_first_bytes(tmp_path: Path) -> None:
    """keep=head 保留开头输出（diff 等需要从头看）。"""
    code = "import sys; sys.stdout.write('A' * 5000); sys.stdout.write('Z' * 5000)"
    result = run_process(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        timeout_s=30,
        max_output_bytes=1000,
        keep="head",
    )
    assert result.stdout == "A" * 1000


def test_stderr_is_bounded_too(tmp_path: Path) -> None:
    """stderr 同样有界捕获，不能被忽略。"""
    code = "import sys; print('e' * 3_000_000, file=sys.stderr)"
    result = run_process(
        [sys.executable, "-c", code], cwd=tmp_path, timeout_s=30, max_output_bytes=5000
    )
    assert result.stderr_truncated is True
    assert len(result.stderr) <= 5000


def test_orphan_descendant_is_killed_with_pid_as_pgid(tmp_path: Path) -> None:
    """直接子进程退出但孙进程仍持有管道时，按 pid=PGID 清理，不能漏杀。"""
    marker = tmp_path / "grandchild-ran.txt"
    grandchild = (
        "import time, pathlib; "
        f"time.sleep(2); pathlib.Path({str(marker)!r}).write_text('alive')"
    )
    parent = (
        "import subprocess, sys; "
        f"subprocess.Popen([sys.executable, '-c', {grandchild!r}]); "
        "print('parent done')"
    )
    result = run_process([sys.executable, "-c", parent], cwd=tmp_path, timeout_s=10)
    assert "parent done" in result.stdout
    # 孙进程继承管道会拖住读取线程，此时 group kill 必须生效
    time.sleep(3)
    assert not marker.exists()


def _interrupt_after_child_starts(marker: Path) -> threading.Thread:
    """等子进程写出 PID 标记后再中断主线程，避免打断 Popen 建组本身。"""

    def run() -> None:
        """轮询标记文件，出现后向主线程发 KeyboardInterrupt。"""
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not marker.exists():
            time.sleep(0.05)
        _thread.interrupt_main()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def test_interrupt_kills_process_group_and_reraises(tmp_path: Path) -> None:
    """Ctrl+C 中断等待时必须整组杀掉子进程再冒泡，不能留下脱离终端的后台命令。"""
    marker = tmp_path / "child.pid"
    # shell 把自身 PID（start_new_session 后即进程组组长）写盘后长睡眠
    watcher = _interrupt_after_child_starts(marker)
    with pytest.raises(KeyboardInterrupt):
        run_shell(f"echo $$ > {marker}; sleep 30", cwd=tmp_path, timeout_s=30)
    watcher.join(timeout=5)

    pid = int(marker.read_text(encoding="utf-8").strip())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    pytest.fail(f"process group leader {pid} survived the interrupt")
