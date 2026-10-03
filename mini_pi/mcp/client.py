"""官方 MCP SDK 的受管异步连接，对同步宿主暴露串行操作。"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import signal
import subprocess
import threading
from datetime import timedelta
from typing import Any, Coroutine, TypeVar

import anyio
from mcp import ClientSession, types
from mcp.shared.exceptions import McpError
from mcp.shared.message import SessionMessage
from mcp.shared.version import SUPPORTED_PROTOCOL_VERSIONS
from pydantic import ValidationError

from mini_pi.errors import ToolError
from mini_pi.security import SecurityPolicy

MAX_FRAME_BYTES = 2_000_000
MAX_STDERR_BYTES = 16_384
T = TypeVar("T")


class MCPError(ToolError):
    """连接、目录或调用的可预期错误，不自动重放外部副作用。"""


class MCPClient:
    """单连接单线程事件循环，生命周期跨查询复用，进程先审批再启动。"""

    def __init__(self, security: SecurityPolicy, server_id: str) -> None:
        """仅保存宿主配置，不启动服务或恢复历史授权。"""
        self.security = security
        self.server_id = server_id
        self.changed = threading.Event()
        self._process: subprocess.Popen[bytes] | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._session: ClientSession | None = None
        self._ready: concurrent.futures.Future[None] = concurrent.futures.Future()
        self._write: anyio.abc.ObjectSendStream[SessionMessage] | None = None
        self._pending: dict[int, str] = {}
        self._stderr = bytearray()
        self._failure: str | None = None
        self._thread_error: BaseException | None = None
        self._secrets: tuple[str, ...] = ()

    @property
    def pid(self) -> int | None:
        """提供宿主进程身份以验证清理，不写入模型消息。"""
        return self._process.pid if self._process is not None else None

    @property
    def connected(self) -> bool:
        """已断开的冻结目录不能用于新调用，任务前应先重新发现。"""
        return (
            self._process is not None
            and self._failure is None
            and self._thread is not None
            and self._thread.is_alive()
        )

    def redact(self, value: str) -> str:
        """配置环境凭据不允许由 server 正文或异常回流。"""
        for secret in sorted(self._secrets, key=len, reverse=True):
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return value

    def _error(self, reason: str) -> MCPError:
        """附有界脱敏 stderr，非法协议正文永不回显。"""
        tail = self.redact(self._stderr.decode("utf-8", errors="replace"))[-2000:]
        return MCPError(self.redact(reason)[:2000] + (f"; stderr: {tail}" if tail else ""))

    def _start(self) -> None:
        """首个目录查询启动官方 SDK，部分初始化失败也回收进程组。"""
        if self._process is not None:
            if self._failure is not None or self._thread is None or not self._thread.is_alive():
                error = self._error(self._failure or "MCP connection closed")
                self.close()
                raise error
            return
        self._secrets = tuple(self.security.secrets)
        self._stderr.clear()
        self._failure = None
        self._thread_error = None
        self._ready = concurrent.futures.Future()
        try:
            self._process = self.security.start_server(self.server_id)
        except OSError as exc:
            raise self._error(f"MCP server could not start: {exc.strerror}") from exc
        try:
            for stream in (self._process.stdin, self._process.stdout, self._process.stderr):
                assert stream is not None
                os.set_blocking(stream.fileno(), False)
            self._thread = threading.Thread(
                target=self._thread_main, name=f"mini-pi-mcp-{self.server_id}", daemon=True
            )
            self._thread.start()
            self._ready.result(timeout=self.security.server_timeout(self.server_id) + 1)
        except BaseException as exc:
            error = self._translate(exc)
            self.close()
            if error is not None:
                raise error from exc
            raise

    def _thread_main(self) -> None:
        """事件循环只创建一次，异常交给同步宿主判断，不能变成线程日志。"""
        try:
            asyncio.run(self._serve())
        except BaseException as exc:
            self._thread_error = exc
            if not self._ready.done():
                self._ready.set_exception(exc)
            elif self._failure is None:
                self._failure = "MCP SDK connection stopped"

    async def _serve(self) -> None:
        """在同一异步任务中进入和退出 SDK 及传输，避免 cancel scope 跨任务。"""
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        read_send, read_receive = anyio.create_memory_object_stream[SessionMessage | Exception](1)
        write_send, write_receive = anyio.create_memory_object_stream[SessionMessage](1)
        self._write = write_send
        async with anyio.create_task_group() as group:
            group.start_soon(self._reader, read_send)
            group.start_soon(self._writer, write_receive)
            group.start_soon(self._drain_stderr)
            async with ClientSession(
                read_receive,
                write_send,
                read_timeout_seconds=timedelta(
                    seconds=self.security.server_timeout(self.server_id)
                ),
                client_info=types.Implementation(name="mini-pi", version="0.1.0"),
            ) as session:
                self._session = session
                result = await session.initialize()
                if (
                    result.protocolVersion not in SUPPORTED_PROTOCOL_VERSIONS
                    or result.capabilities.tools is None
                ):
                    raise MCPError("MCP server has incompatible version or no tools capability")
                self._ready.set_result(None)
                await self._stop.wait()
            group.cancel_scope.cancel()

    async def _read(self, fd: int) -> bytes:
        """非阻塞管道读在异步线程内等待，取消不会留下阻塞读取线程。"""
        while True:
            await anyio.wait_readable(fd)
            try:
                return os.read(fd, 65536)
            except BlockingIOError:
                continue

    async def _reader(self, output: anyio.abc.ObjectSendStream[SessionMessage | Exception]) -> None:
        """逐行有界解析 stdio，错误 id 和普通日志在进入 SDK 前拒绝。"""
        assert self._process is not None and self._process.stdout is not None
        buffer = bytearray()
        try:
            while True:
                chunk = await self._read(self._process.stdout.fileno())
                if not chunk:
                    raise MCPError("MCP server closed stdout")
                buffer.extend(chunk)
                while (boundary := buffer.find(b"\n")) >= 0:
                    if boundary > MAX_FRAME_BYTES:
                        raise MCPError("MCP frame exceeds byte limit")
                    line = bytes(buffer[:boundary])
                    del buffer[: boundary + 1]
                    await self._incoming(line, output)
                if len(buffer) > MAX_FRAME_BYTES:
                    raise MCPError("MCP frame exceeds byte limit")
        except (MCPError, OSError, ValueError, UnicodeError, ValidationError, RecursionError):
            # 不打印 SDK/JSON 校验异常，其输入可能含凭据和未截断的 server 正文。
            self._failure = "invalid MCP protocol, EOF or transport failure"
        finally:
            await output.aclose()
            assert self._stop is not None
            self._stop.set()

    async def _incoming(
        self, line: bytes, output: anyio.abc.ObjectSendStream[SessionMessage | Exception]
    ) -> None:
        """反向请求仅响应 ping，其他明确拒绝；通知不累计为无限队列。"""
        value = json.loads(line, parse_constant=self._reject_constant)
        if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
            raise MCPError("invalid MCP envelope")
        if "method" in value:
            method = value["method"]
            if not isinstance(method, str) or (
                "params" in value and not isinstance(value["params"], dict)
            ):
                raise MCPError("invalid MCP notification/request")
            if "id" in value:
                if type(value["id"]) not in {int, str}:
                    raise MCPError("invalid MCP server request id")
                response = (
                    types.JSONRPCResponse(jsonrpc="2.0", id=value["id"], result={})
                    if method == "ping"
                    else types.JSONRPCError(
                        jsonrpc="2.0",
                        id=value["id"],
                        error=types.ErrorData(
                            code=-32601,
                            message="mini-pi supports tools only; client requests are unavailable",
                        ),
                    )
                )
                assert self._write is not None
                await self._write.send(SessionMessage(types.JSONRPCMessage(response)))
            elif method == "notifications/tools/list_changed":
                self.changed.set()
            # 日志/进度/资源通知只丢弃，不执行 URI、不把指令提升为宿主规则。
            return
        request_id = value.get("id")
        if (
            type(request_id) is not int
            or request_id not in self._pending
            or ("result" in value) == ("error" in value)
        ):
            raise MCPError("unexpected MCP response id or envelope")
        method = self._pending.pop(request_id)
        if "result" in value:
            # SDK 的宽松解析会转换错误类型，宿主先按响应契约拒绝非法事实。
            result_type = {
                "initialize": types.InitializeResult,
                "tools/list": types.ListToolsResult,
                "tools/call": types.CallToolResult,
            }[method]
            result_type.model_validate(value["result"], strict=True)
        message = types.JSONRPCMessage.model_validate(value)
        await output.send(SessionMessage(message))

    @staticmethod
    def _reject_constant(value: str) -> None:
        """标准 JSON 不允许非有限数，协议输入不能隐式宽松解析。"""
        raise ValueError("invalid JSON constant")

    async def _writer(self, messages: anyio.abc.ObjectReceiveStream[SessionMessage]) -> None:
        """写完整逐行 JSON，阻塞的 stdin 由调用的整体时限终止。"""
        assert self._process is not None and self._process.stdin is not None
        fd = self._process.stdin.fileno()
        try:
            async for message in messages:
                data = (
                    message.message.model_dump_json(by_alias=True, exclude_none=True).encode(
                        "utf-8"
                    )
                    + b"\n"
                )
                if len(data) > MAX_FRAME_BYTES:
                    raise MCPError("MCP outgoing frame exceeds byte limit")
                root = message.message.root
                if isinstance(root, types.JSONRPCRequest):
                    self._pending[root.id] = root.method
                remaining = memoryview(data)
                while remaining:
                    await anyio.wait_writable(fd)
                    try:
                        size = os.write(fd, remaining)
                    except BlockingIOError:
                        continue
                    remaining = remaining[size:]
        except (MCPError, OSError):
            self._failure = "MCP write failed"
            assert self._stop is not None
            self._stop.set()

    async def _drain_stderr(self) -> None:
        """排空 stderr，只保留 16 KB 尾部，不让 server 日志写满阻塞。"""
        assert self._process is not None and self._process.stderr is not None
        try:
            while chunk := await self._read(self._process.stderr.fileno()):
                self._stderr.extend(chunk)
                del self._stderr[:-MAX_STDERR_BYTES]
        except (OSError, ValueError):
            return

    def _translate(self, exc: BaseException) -> MCPError | None:
        """仅已知协议/环境异常转 ToolError，程序缺陷原样暴露。"""
        if isinstance(exc, BaseExceptionGroup):
            errors = [self._translate(item) for item in exc.exceptions]
            return self._error(self._failure or "MCP SDK protocol failure") if all(errors) else None
        if isinstance(
            exc,
            (
                MCPError,
                McpError,
                ValidationError,
                TimeoutError,
                OSError,
                concurrent.futures.CancelledError,
            ),
        ):
            return self._error(
                self._failure
                or (
                    str(exc)
                    if isinstance(exc, (McpError, MCPError))
                    else "MCP operation failed or timed out"
                )
            )
        if isinstance(exc, RuntimeError) and str(exc).startswith(
            "Unsupported protocol version from the server:"
        ):
            return self._error("unsupported MCP protocol version")
        return None

    def _run(self, operation: Coroutine[Any, Any, T]) -> T:
        """在唯一事件循环提交串行操作；失败/取消清理且绝不自动重放。"""
        assert self._loop is not None
        try:
            future = asyncio.run_coroutine_threadsafe(operation, self._loop)
        except RuntimeError as exc:
            operation.close()
            if not self._loop.is_closed():
                raise
            self.close()
            raise self._error("MCP connection closed before request") from exc
        try:
            return future.result(timeout=self.security.server_timeout(self.server_id) + 1)
        except BaseException as exc:
            error = self._translate(exc)
            self._cancel()
            future.cancel()
            self.close()
            if error is not None:
                raise error from exc
            raise

    async def _cancel_request(self) -> None:
        """尽力发取消通知，不依赖远端及时配合清理。"""
        if self._session is not None:
            for request_id in tuple(self._pending):
                await self._session.send_notification(
                    types.ClientNotification(
                        types.CancelledNotification(
                            params=types.CancelledNotificationParams(
                                requestId=request_id, reason="host cancelled or timed out"
                            )
                        )
                    )
                )

    def _cancel(self) -> None:
        """取消通知等待最多 0.2 秒，之后依旧强制清理整个进程组。"""
        if self._loop is not None and self._thread is not None and self._thread.is_alive():
            operation = self._cancel_request()
            try:
                future = asyncio.run_coroutine_threadsafe(operation, self._loop)
            except RuntimeError:
                operation.close()
                # 远端断开可能先结束事件循环；这时只需继续清理进程。
                if self._loop.is_closed():
                    return
                raise
            try:
                future.result(timeout=0.2)
            except (
                TimeoutError,
                concurrent.futures.CancelledError,
                McpError,
                anyio.BrokenResourceError,
                anyio.ClosedResourceError,
            ):
                future.cancel()

    async def _list(self) -> list[types.Tool]:
        """有界分页发现目录，绕过 SDK 自动缓存和后续隐式 schema 再发现。"""
        assert self._session is not None
        tools: list[types.Tool] = []
        cursors: set[str] = set()
        cursor: str | None = None
        for _ in range(16):
            params = types.PaginatedRequestParams(cursor=cursor) if cursor is not None else None
            result = await self._session.send_request(
                types.ClientRequest(types.ListToolsRequest(params=params)), types.ListToolsResult
            )
            tools.extend(result.tools)
            if len(tools) > 256:
                raise MCPError("MCP discovered tool count exceeds 256")
            cursor = result.nextCursor
            if cursor is None:
                if len({tool.name for tool in tools}) != len(tools):
                    raise MCPError("duplicate MCP tool name")
                return tools
            if not cursor or len(cursor) > 4096 or cursor in cursors:
                raise MCPError("invalid or repeated MCP pagination cursor")
            cursors.add(cursor)
        raise MCPError("MCP tools/list exceeded 16 pages")

    def list_tools(self) -> list[types.Tool]:
        """显式刷新或首次发现，只进行一次真实分页往返。"""
        self._start()
        self.changed.clear()
        return self._run(self._list())

    async def _call(self, name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        """按冻结原名调用一次；输出 schema 由宿主本地验证，SDK 不另发 list。"""
        assert self._session is not None
        request = types.ClientRequest(
            types.CallToolRequest(
                params=types.CallToolRequestParams(name=name, arguments=arguments)
            )
        )
        return await self._session.send_request(request, types.CallToolResult)

    def call_tool(self, name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        """批准后的单次调用只使用已发现连接，失效目录要求重新发现而非盲目重启。"""
        if not self.connected:
            raise self._error("MCP connection unavailable; reload tools before calling again")
        return self._run(self._call(name, arguments))

    def close(self) -> None:
        """关闭输入并限时退出，必要时 TERM/KILL 整个进程组并回收事件循环。"""
        process = self._process
        if process is None:
            return
        while True:
            try:
                if process.stdin is not None:
                    process.stdin.close()
                if (
                    self._loop is not None
                    and self._stop is not None
                    and self._thread is not None
                    and self._thread.is_alive()
                ):
                    try:
                        self._loop.call_soon_threadsafe(self._stop.set)
                    except RuntimeError:
                        if not self._loop.is_closed():
                            raise
                if self._thread is not None:
                    self._thread.join(timeout=2)
                try:
                    process.wait(timeout=0.3)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    try:
                        process.wait(timeout=0.3)
                    except subprocess.TimeoutExpired:
                        pass
                # 直接进程退出也不能留下持有管道的同组孙进程。
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=2)
                for stream in (process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()
                if self._thread is not None and self._thread.is_alive():
                    raise RuntimeError("MCP event loop failed to stop")
                self._process = None
                self._thread = None
                self._loop = None
                self._stop = None
                self._session = None
                self._write = None
                self._pending.clear()
                error = self._thread_error
                self._thread_error = None
                # 后台缺陷在释放资源后回到宿主线程，不能伪装成普通协议错误。
                if error is not None and self._translate(error) is None:
                    raise error
                return
            except KeyboardInterrupt:
                continue
