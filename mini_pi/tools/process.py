"""进程执行：统一处理超时与进程组清理。"""

from __future__ import annotations

import os
import signal
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProcessResult:
    """命令执行结果；timed_out 为 True 时 exit_code 固定为 -1。"""

    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


def run_process(argv: Sequence[str], *, cwd: Path, timeout_s: int) -> ProcessResult:
    """以 argv 形式执行，不经 shell，适合 git 等固定命令。"""
    process = subprocess.Popen(
        list(argv),
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        # 独立进程组，超时可整组杀掉，避免子孙进程逃逸
        start_new_session=True,
    )
    return _communicate(process, timeout_s)


def run_shell(command: str, *, cwd: Path, timeout_s: int) -> ProcessResult:
    """以 shell 形式执行任意命令（支持管道/重定向）。"""
    process = subprocess.Popen(
        command,
        cwd=cwd,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    return _communicate(process, timeout_s)


def _communicate(process: subprocess.Popen[str], timeout_s: int) -> ProcessResult:
    try:
        stdout, stderr = process.communicate(timeout=timeout_s)
        return ProcessResult(
            exit_code=process.returncode,
            stdout=stdout or "",
            stderr=stderr or "",
        )
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except ProcessLookupError:
            # 进程可能恰好在超时瞬间自行退出
            pass
        stdout, stderr = process.communicate()
        return ProcessResult(
            exit_code=-1,
            stdout=stdout or "",
            stderr=stderr or "",
            timed_out=True,
        )
