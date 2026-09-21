"""进程执行：有界捕获输出，统一处理超时与进程组清理。"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Literal

# 单个流（stdout / stderr）的内存上限，超出部分按 keep 方向丢弃
DEFAULT_MAX_OUTPUT_BYTES = 1_000_000


@dataclass(frozen=True)
class ProcessResult:
    """命令执行结果；timed_out 为 True 时 exit_code 固定为 -1。"""

    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    stdout_truncated: bool = False
    stderr_truncated: bool = False


class _BoundedCapture:
    """固定内存的字节缓冲：head 保留开头，tail 保留结尾。"""

    def __init__(self, max_bytes: int, keep: Literal["head", "tail"]) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be > 0")
        self._max = max_bytes
        self._keep = keep
        self._buffer = bytearray()
        self._truncated = False

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        if self._keep == "head":
            room = self._max - len(self._buffer)
            if room <= 0:
                self._truncated = True
                return
            if len(chunk) > room:
                self._buffer.extend(chunk[:room])
                self._truncated = True
                return
            self._buffer.extend(chunk)
            return
        if len(chunk) >= self._max:
            self._buffer = bytearray(chunk[-self._max :])
            self._truncated = True
            return
        overflow = len(self._buffer) + len(chunk) - self._max
        if overflow > 0:
            del self._buffer[:overflow]
            self._truncated = True
        self._buffer.extend(chunk)

    def result(self) -> tuple[str, bool]:
        # 截断可能落在多字节字符中间，用 replace 容错解码
        return self._buffer.decode("utf-8", errors="replace"), self._truncated


def run_process(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout_s: int,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    keep: Literal["head", "tail"] = "tail",
) -> ProcessResult:
    """以 argv 形式执行，不经 shell，适合 git 等固定命令。"""
    process = subprocess.Popen(
        list(argv),
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        # 独立进程组，超时可整组杀掉，避免子孙进程逃逸
        start_new_session=True,
    )
    return _run_bounded(process, timeout_s, max_output_bytes, keep)


def run_shell(
    command: str,
    *,
    cwd: Path,
    timeout_s: int,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    keep: Literal["head", "tail"] = "tail",
) -> ProcessResult:
    """以 shell 形式执行任意命令（支持管道/重定向）。"""
    process = subprocess.Popen(
        command,
        cwd=cwd,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        start_new_session=True,
    )
    return _run_bounded(process, timeout_s, max_output_bytes, keep)


def _run_bounded(
    process: subprocess.Popen[bytes],
    timeout_s: int,
    max_output_bytes: int,
    keep: Literal["head", "tail"],
) -> ProcessResult:
    stdout_capture = _BoundedCapture(max_output_bytes, keep)
    stderr_capture = _BoundedCapture(max_output_bytes, keep)
    # 用读取线程持续排空管道，避免子进程因管道写满而阻塞
    threads = [
        threading.Thread(target=_pump, args=(process.stdout, stdout_capture), daemon=True),
        threading.Thread(target=_pump, args=(process.stderr, stderr_capture), daemon=True),
    ]
    for thread in threads:
        thread.start()

    timed_out = False
    try:
        process.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_group(process)
        process.wait()

    _join_with_deadline(threads, 1.0)
    if any(thread.is_alive() for thread in threads):
        # 直接子进程已退出，但孙进程仍持有管道：清掉进程组后再收尾
        _kill_group(process)
        _join_with_deadline(threads, 1.0)
    stdout, stdout_truncated = stdout_capture.result()
    stderr, stderr_truncated = stderr_capture.result()
    _close(process.stdout)
    _close(process.stderr)
    return ProcessResult(
        exit_code=-1 if timed_out else process.returncode,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
    )


def _join_with_deadline(threads: list[threading.Thread], timeout_s: float) -> None:
    """多个线程共享同一个等待预算，避免逐个 join 把延迟翻倍。"""
    deadline = time.monotonic() + timeout_s
    for thread in threads:
        thread.join(timeout=max(0.0, deadline - time.monotonic()))


def _pump(stream: IO[bytes] | None, capture: _BoundedCapture) -> None:
    """从管道读取原始字节并喂给有界缓冲，直到 EOF。"""
    if stream is None:
        return
    fd = stream.fileno()
    while True:
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            break
        if not chunk:
            break
        capture.feed(chunk)


def _kill_group(process: subprocess.Popen[bytes]) -> None:
    """start_new_session 保证 PID == PGID；子进程被回收后仍按该值清理残留成员。

    不用 os.getpgid：直接子进程退出并被 wait() 回收后，PID 可能已查不到，
    getpgid 会抛 ProcessLookupError，反而漏杀仍持有管道的孙进程。
    """
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        # 进程组已空（全部自然退出）
        pass


def _close(stream: IO[bytes] | None) -> None:
    if stream is None:
        return
    try:
        stream.close()
    except OSError:
        pass
