"""LSP Content-Length 分帧与串行双向 stdio，读写和退出均有时限。"""

from __future__ import annotations

import json
import os
import re
import select
import signal
import subprocess
import threading
import time
from typing import Any

from mini_pi.errors import ToolError

MAX_FRAME_BYTES = 2_000_000
MAX_HEADER_BYTES = 8192
MAX_STDERR_BYTES = 16_384


class LspError(ToolError):
    """可预期的语义查询或新鲜度错误，模型可以显式调整下一次调用。"""


class LspProtocolError(LspError):
    """连接状态不可信，调用方必须关闭进程且不能自动重放请求。"""


class StdioTransport:
    """管理一个已审批的进程，以非阻塞管道实现有界收发和 stderr 捕获。"""

    def __init__(self, process: subprocess.Popen[bytes], secrets: tuple[str, ...] = ()) -> None:
        """接管进程管道，stdout 只用于协议，stderr 单独排空并保留尾部。"""
        if process.stdin is None or process.stdout is None or process.stderr is None:
            raise ValueError("LSP process requires stdin/stdout/stderr pipes")
        self.process = process
        self._input = process.stdin.fileno()
        self._output = process.stdout.fileno()
        self._buffer = bytearray()
        self._secrets = secrets
        self._stderr = bytearray()
        self._stderr_lock = threading.Lock()
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._closed = False
        try:
            os.set_blocking(self._input, False)
            os.set_blocking(self._output, False)
            self._stderr_thread.start()
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=2)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()
            raise

    def redact(self, value: str) -> str:
        """环境凭据先脱敏，不能由错误或结果回流至模型和 JSONL。"""
        for secret in sorted(self._secrets, key=len, reverse=True):
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return value

    def _drain_stderr(self) -> None:
        """持续排空错误管道，防止 server 因日志写满而挂起。"""
        assert self.process.stderr is not None
        while True:
            try:
                data = os.read(self.process.stderr.fileno(), 8192)
            except (OSError, ValueError):
                return
            if not data:
                return
            with self._stderr_lock:
                self._stderr.extend(data)
                del self._stderr[:-MAX_STDERR_BYTES]

    def _failure(self, reason: str) -> LspProtocolError:
        """携带脱敏且有界的真实 stderr，不回显非法协议正文。"""
        with self._stderr_lock:
            stderr = self._stderr.decode("utf-8", errors="replace")
        detail = self.redact(stderr)[-2048:]
        return LspProtocolError(f"{reason}" + (f"; stderr: {detail}" if detail else ""))

    def send(self, message: dict[str, Any], *, timeout: float) -> None:
        """按字节长度写完整帧，server 不读 stdin 时也按时失败。"""
        body = json.dumps(message, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(body) > MAX_FRAME_BYTES:
            # didOpen/didChange 的宿主状态可能已推进，拒发后必须废弃连接再同步。
            raise LspProtocolError("LSP outgoing frame exceeds byte limit")
        data = memoryview(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
        deadline = time.monotonic() + timeout
        try:
            while data:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([], [self._input], [], remaining)[1]:
                    raise self._failure("LSP write timed out")
                try:
                    written = os.write(self._input, data)
                except BlockingIOError:
                    continue
                data = data[written:]
        except OSError as exc:
            raise self._failure(f"LSP write failed: {exc.strerror}") from exc

    def receive(self, *, timeout: float) -> dict[str, Any]:
        """组合分片并读取单个 JSON-RPC 帧，拒绝超大、残缺或非协议输出。"""
        deadline = time.monotonic() + timeout
        while True:
            boundary = self._buffer.find(b"\r\n\r\n")
            if boundary == -1 and len(self._buffer) > MAX_HEADER_BYTES:
                raise self._failure("LSP header exceeds byte limit")
            if boundary >= 0:
                if boundary > MAX_HEADER_BYTES:
                    raise self._failure("LSP header exceeds byte limit")
                size = self._content_length(bytes(self._buffer[:boundary]))
                end = boundary + 4 + size
                if len(self._buffer) >= end:
                    body = bytes(self._buffer[boundary + 4 : end])
                    del self._buffer[:end]
                    try:
                        message = json.loads(body, parse_constant=self._reject_constant)
                    except (ValueError, UnicodeError) as exc:
                        raise self._failure("invalid LSP JSON") from exc
                    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                        raise self._failure("invalid LSP JSON-RPC envelope")
                    return message
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self._output], [], [], remaining)[0]:
                raise self._failure("LSP response timed out")
            try:
                data = os.read(self._output, 65536)
            except BlockingIOError:
                continue
            except OSError as exc:
                raise self._failure(f"LSP read failed: {exc.strerror}") from exc
            if not data:
                raise self._failure(f"LSP process closed stdout (exit={self.process.poll()})")
            self._buffer.extend(data)

    @staticmethod
    def _reject_constant(value: str) -> None:
        """JSON-RPC 不允许 NaN 或 Infinity，不能按 Python 扩展解释。"""
        raise ValueError(f"invalid JSON constant: {value}")

    def _content_length(self, header: bytes) -> int:
        """严格读取唯一 Content-Length，并拒绝非 UTF-8 内容编码。"""
        try:
            lines = header.decode("ascii").split("\r\n")
        except UnicodeError as exc:
            raise self._failure("LSP header is not ASCII") from exc
        fields: dict[str, str] = {}
        for line in lines:
            name, separator, value = line.partition(":")
            key = name.lower()
            if not separator or key in fields:
                raise self._failure("malformed or duplicate LSP header")
            fields[key] = value.strip()
        size = fields.get("content-length", "")
        if not re.fullmatch(r"[0-9]{1,8}", size) or not 0 < int(size) <= MAX_FRAME_BYTES:
            raise self._failure("invalid or oversized LSP Content-Length")
        content_type = fields.get("content-type", "").lower()
        if "charset=" in content_type and content_type.split("charset=", 1)[1].strip() not in {
            "utf-8",
            "utf8",
        }:
            raise self._failure("unsupported LSP charset")
        return int(size)

    def close(self) -> None:
        """关闭整个进程组并回收管道；连续中断不能留下进程或捕获线程。"""
        if self._closed:
            return
        while True:
            try:
                # 即使直接进程已退出，仍清理同组持有管道的孙进程。
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.process.wait(timeout=2)
                self._stderr_thread.join(timeout=1)
                for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                    if stream is not None:
                        stream.close()
                self._closed = True
                return
            except KeyboardInterrupt:
                continue
