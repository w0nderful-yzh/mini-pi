from __future__ import annotations

import sys
import time
from pathlib import Path

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
