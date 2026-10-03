"""单工作区只读 LSP：懒启动、磁盘同步、版本诊断与明确资源生命周期。"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from mini_pi.errors import WorkspaceViolationError
from mini_pi.lsp.positions import convert_range, lines, to_wire, units
from mini_pi.lsp.transport import LspError, LspProtocolError, StdioTransport
from mini_pi.security import SecurityPolicy, fingerprint
from mini_pi.workspace.workspace import Workspace

MAX_FILE_BYTES = 1_000_000
MAX_DOCUMENTS = 32
MAX_RESULTS = 100
MAX_RESULT_BYTES = 50_000


@dataclass(slots=True)
class Document:
    """显式打开文件的完整文本和递增版本，不扫描工作区或依赖 modified_files。"""

    text: str
    version: int


@dataclass(frozen=True, slots=True)
class Diagnostics:
    """server 推送的有界诊断快照，只有当前文档版本可以采纳。"""

    version: int | None
    items: tuple[dict[str, Any], ...]
    truncated: bool


class LspClient:
    """串行查询本地 Python server；不重试业务请求，不支持编辑或命令。"""

    def __init__(self, workspace: Workspace, security: SecurityPolicy, server_id: str) -> None:
        """保存宿主依赖，注册工具时不启动进程或读取仓库文件。"""
        self.workspace = workspace
        self._security = security
        self._server_id = server_id
        self._transport: StdioTransport | None = None
        self._next_id = 0
        self._documents: dict[str, Document] = {}
        self._diagnostics: dict[str, Diagnostics] = {}
        self._capabilities: dict[str, Any] = {}
        self._encoding = "utf-16"
        self._sync_kind = 0
        self._initialized = False

    @property
    def pid(self) -> int | None:
        """向宿主提供当前进程身份供生命周期检查，不写入模型消息。"""
        return self._transport.process.pid if self._transport is not None else None

    def _read(self, path: str | Path) -> tuple[str, str]:
        """经 Workspace 有界读取完整 Python 文档，失败不自动修复输入路径。"""
        resolved = self.workspace.resolve(path)
        if resolved.suffix not in {".py", ".pyi"}:
            raise LspError(
                "the configured LSP client currently supports Python .py/.pyi files only"
            )
        try:
            raw = self.workspace.read_bytes(path, max_bytes=MAX_FILE_BYTES + 1)
            if len(raw) > MAX_FILE_BYTES:
                raise LspError("LSP document exceeds byte limit")
            return resolved.as_uri(), raw.decode("utf-8")
        except (OSError, UnicodeError) as exc:
            raise LspError(f"cannot read UTF-8 LSP document: {resolved.name}") from exc

    def _start(self) -> None:
        """首个有效查询审批并初始化，任何部分启动失败都收回进程。"""
        if self._transport is not None:
            return
        secrets = tuple(self._security.secrets)
        try:
            process = self._security.start_server(self._server_id)
        except OSError as exc:
            raise LspError(f"LSP server could not start: {exc.strerror}") from exc
        self._transport = StdioTransport(process, secrets)
        try:
            result = self._request(
                "initialize",
                {
                    "processId": os.getpid(),
                    "rootUri": self.workspace.root.as_uri(),
                    "capabilities": {
                        "general": {"positionEncodings": ["utf-16", "utf-8", "utf-32"]},
                        "workspace": {
                            "configuration": True,
                            "workspaceFolders": True,
                            "applyEdit": False,
                        },
                        "textDocument": {
                            "synchronization": {"dynamicRegistration": False},
                            "definition": {"dynamicRegistration": False, "linkSupport": True},
                            "references": {"dynamicRegistration": False},
                            "publishDiagnostics": {"versionSupport": True},
                            "diagnostic": {
                                "dynamicRegistration": False,
                                "relatedDocumentSupport": False,
                            },
                        },
                    },
                    "workspaceFolders": self._folders(),
                },
            )
            if not isinstance(result, dict) or not isinstance(result.get("capabilities"), dict):
                raise LspProtocolError("initialize did not return capabilities")
            self._capabilities = result["capabilities"]
            self._encoding = self._capabilities.get("positionEncoding", "utf-16")
            if not isinstance(self._encoding, str) or self._encoding not in {
                "utf-16",
                "utf-8",
                "utf-32",
            }:
                raise LspProtocolError("server selected an unsupported position encoding")
            sync = self._capabilities.get("textDocumentSync")
            if type(sync) is int:
                self._sync_kind = sync
            elif isinstance(sync, dict) and sync.get("openClose") is True:
                self._sync_kind = sync.get("change", 0)
            if type(self._sync_kind) is not int or self._sync_kind not in {1, 2}:
                raise LspError(
                    "server must support document open/close and full or incremental synchronization"
                )
            self._notify("initialized", {})
            self._initialized = True
        except BaseException:
            self.close(force=True)
            raise

    @property
    def _timeout(self) -> float:
        """所有操作复用显式用户配置的等待上限。"""
        return self._security.server_timeout(self._server_id)

    def _folders(self) -> list[dict[str, str]]:
        """仅声明当前工作区，不因 server 请求扩大根目录。"""
        return [{"uri": self.workspace.root.as_uri(), "name": self.workspace.root.name}]

    def _notify(self, method: str, params: dict[str, Any], *, timeout: float | None = None) -> None:
        """同步发送通知，通知本身没有响应 id。"""
        assert self._transport is not None
        self._transport.send(
            {"jsonrpc": "2.0", "method": method, "params": params}, timeout=timeout or self._timeout
        )

    def _request(
        self, method: str, params: dict[str, Any] | None, *, timeout: float | None = None
    ) -> Any:
        """等待当前唯一请求，期间处理交错通知与 server 控制请求。"""
        assert self._transport is not None
        self._next_id += 1
        request_id = self._next_id
        duration = timeout or self._timeout
        deadline = time.monotonic() + duration
        self._transport.send(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
            timeout=duration,
        )
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise LspProtocolError("LSP request timed out")
                message = self._transport.receive(timeout=remaining)
                if "method" in message:
                    self._dispatch(message, timeout=max(0.001, deadline - time.monotonic()))
                    continue
                if type(message.get("id")) is not int or message["id"] != request_id:
                    raise LspProtocolError("unexpected LSP response id")
                if ("error" in message) == ("result" in message):
                    raise LspProtocolError("LSP response must contain exactly result or error")
                if "error" in message:
                    error = message["error"]
                    if (
                        not isinstance(error, dict)
                        or type(error.get("code")) is not int
                        or not isinstance(error.get("message"), str)
                    ):
                        raise LspProtocolError("invalid LSP error response")
                    raise LspError(
                        f"LSP {method} failed ({error['code']}): {self._transport.redact(error['message'])[:2000]}"
                    )
                return message["result"]
        except KeyboardInterrupt:
            try:
                self._notify("$/cancelRequest", {"id": request_id}, timeout=0.2)
            except LspError:
                # 取消通知不能发出时仍强制关闭，绝不依赖 server 配合来清理。
                pass
            self.close(force=True)
            raise

    def _dispatch(self, message: dict[str, Any], *, timeout: float) -> None:
        """按已声明能力回应控制请求，不执行 server 发来的文件编辑或命令。"""
        assert self._transport is not None
        method = message.get("method")
        if not isinstance(method, str):
            raise LspProtocolError("invalid LSP method")
        params = message.get("params", {})
        if "id" in message:
            if type(message["id"]) not in {int, str}:
                raise LspProtocolError("invalid server request id")
            response: dict[str, Any] = {"jsonrpc": "2.0", "id": message["id"]}
            if (
                method == "workspace/configuration"
                and isinstance(params, dict)
                and isinstance(params.get("items"), list)
                and len(params["items"]) <= 128
            ):
                values = []
                for item in params["items"]:
                    if not isinstance(item, dict):
                        raise LspProtocolError("invalid configuration item")
                    analysis = {
                        "diagnosticMode": "openFilesOnly",
                        "typeCheckingMode": "basic",
                        "autoSearchPaths": False,
                    }
                    section = item.get("section")
                    values.append(
                        {"analysis": analysis}
                        if section == "python"
                        else analysis
                        if section == "python.analysis"
                        else None
                    )
                response["result"] = values
            elif method == "workspace/workspaceFolders":
                response["result"] = self._folders()
            else:
                response["error"] = {
                    "code": -32601,
                    "message": "Unsupported readonly client request",
                }
            self._transport.send(response, timeout=timeout)
        elif method == "textDocument/publishDiagnostics":
            if not isinstance(params, dict):
                raise LspProtocolError("invalid diagnostics notification")
            uri, version, items = (
                params.get("uri"),
                params.get("version"),
                params.get("diagnostics"),
            )
            if (
                not isinstance(uri, str)
                or not isinstance(items, list)
                or (version is not None and type(version) is not int)
            ):
                raise LspProtocolError("invalid diagnostics fields")
            document = self._documents.get(uri)
            if document is not None and (version is None or version == document.version):
                if not all(isinstance(item, dict) for item in items[:MAX_RESULTS]):
                    raise LspProtocolError("invalid diagnostic item")
                self._diagnostics[uri] = Diagnostics(
                    version, tuple(items[:MAX_RESULTS]), len(items) > MAX_RESULTS
                )
        # 未声明的日志、进度和其他通知不改变宿主权限，也不累计到无限队列。

    def _change(self, uri: str, text: str) -> None:
        """以完整文本或完整范围替换同步，增量协议也不生成猜测性局部编辑。"""
        document = self._documents[uri]
        changes: dict[str, Any] = {"text": text}
        if self._sync_kind == 2:
            previous_lines = lines(document.text)
            changes["range"] = {
                "start": {"line": 0, "character": 0},
                "end": {
                    "line": len(previous_lines) - 1,
                    "character": units(previous_lines[-1], self._encoding),
                },
            }
        document.version += 1
        document.text = text
        self._notify(
            "textDocument/didChange",
            {
                "textDocument": {"uri": uri, "version": document.version},
                "contentChanges": [changes],
            },
        )

    def _synchronize(self, path: str) -> tuple[str, Document]:
        """重读已打开的文件和当前文件，磁盘变化不依赖工具改动清单。"""
        uri, text = self._read(path)
        if uri not in self._documents and len(self._documents) >= MAX_DOCUMENTS:
            raise LspError("too many open LSP documents; close the runtime before opening more")
        changed = False
        target_changed = False
        for opened, document in tuple(self._documents.items()):
            _, current = self._read(self._uri_path(opened))
            if current != document.text:
                self._change(opened, current)
                changed = True
                target_changed |= opened == uri
        if uri not in self._documents:
            self._documents[uri] = Document(text, 1)
            self._notify(
                "textDocument/didOpen",
                {"textDocument": {"uri": uri, "languageId": "python", "version": 1, "text": text}},
            )
        elif changed and not target_changed:
            # 依赖文件变化后也提高查询文件版本，使旧的同版本诊断不能冒充新分析。
            self._change(uri, text)
        if changed:
            self._diagnostics.clear()
        return uri, self._documents[uri]

    @staticmethod
    def _uri_path(uri: str) -> Path:
        """只接受本机绝对 file URI，拒绝远程主机或其他 URI scheme。"""
        try:
            parsed = urlsplit(uri)
            decoded = unquote(parsed.path, errors="strict")
        except (ValueError, UnicodeError) as exc:
            raise LspProtocolError("malformed LSP file URI") from exc
        if "\x00" in decoded:
            raise LspProtocolError("invalid character in LSP file URI")
        if (
            parsed.scheme != "file"
            or parsed.netloc not in {"", "localhost"}
            or parsed.query
            or parsed.fragment
        ):
            raise LspError("not a local file URI")
        path = Path(decoded)
        if not path.is_absolute():
            raise LspError("file URI must be absolute")
        return path

    def _ensure_current(self) -> None:
        """等待中发生外部改动时拒绝旧结果，下次显式查询再同步。"""
        for uri, document in self._documents.items():
            if self._read(self._uri_path(uri))[1] != document.text:
                raise LspError(
                    "open documents changed while querying; query again for current content"
                )

    def _location(self, value: Any) -> dict[str, Any]:
        """工作区内目标转换字符列，工作区外目标仅标记受限且不读取。"""
        if not isinstance(value, dict):
            raise LspProtocolError("invalid LSP location")
        uri = value.get("targetUri", value.get("uri"))
        wire_range = (
            value.get("targetSelectionRange") if "targetUri" in value else value.get("range")
        )
        if not isinstance(uri, str) or len(uri) > 4096 or not isinstance(wire_range, dict):
            raise LspProtocolError("invalid LSP location fields")
        try:
            path = self.workspace.resolve(self._uri_path(uri))
        except LspProtocolError:
            raise
        except (LspError, WorkspaceViolationError):
            return {
                "uri": uri,
                "restricted": True,
                "reason": "target is outside the local workspace",
            }
        _, text = self._read(path)
        return {
            "path": self.workspace.relative(path),
            "range": convert_range(text, wire_range, self._encoding),
            "restricted": False,
        }

    def _result(self, payload: dict[str, Any], key: str, *, already_truncated: bool = False) -> str:
        """保持完整 JSON 并截断条目数和字节数，所有 server 字符串先脱敏。"""
        items = payload[key]
        payload["truncated"] = already_truncated or len(items) > MAX_RESULTS
        payload[key] = items[:MAX_RESULTS]
        while True:
            encoded = json.dumps(payload, ensure_ascii=False)
            if self._transport is not None:
                # 序列化前后的特殊字符均可能存在于凭据中，逐个叶值脱敏见下方递归。
                encoded = json.dumps(self._redact_payload(payload), ensure_ascii=False)
            if len(encoded.encode("utf-8")) <= MAX_RESULT_BYTES:
                return encoded
            if not payload[key]:
                raise LspError("LSP result metadata exceeds byte limit")
            payload[key].pop()
            payload["truncated"] = True

    def _redact_payload(self, value: Any) -> Any:
        """递归脱敏再序列化，避免引号和 Unicode 转义改变凭据匹配。"""
        assert self._transport is not None
        if isinstance(value, str):
            return self._transport.redact(value)
        if isinstance(value, list):
            return [self._redact_payload(item) for item in value]
        if isinstance(value, dict):
            return {key: self._redact_payload(item) for key, item in value.items()}
        return value

    def locations(self, method: str, path: str, line: int, column: int) -> str:
        """读取当前位置的定义或引用；协议异常和取消后关闭，普通查询错误不伪装成功。"""
        if method not in {"textDocument/definition", "textDocument/references"}:
            raise ValueError("unsupported readonly LSP method")
        _, initial_text = self._read(path)
        to_wire(initial_text, line, column, "utf-16")
        try:
            self._start()
            capability = (
                "definitionProvider"
                if method == "textDocument/definition"
                else "referencesProvider"
            )
            declared = self._capabilities.get(capability)
            if declared is not True and not isinstance(declared, dict):
                raise LspError(f"server does not support {method}; use search/read explicitly")
            uri, document = self._synchronize(path)
            params: dict[str, Any] = {
                "textDocument": {"uri": uri},
                "position": to_wire(document.text, line, column, self._encoding),
            }
            if method == "textDocument/references":
                params["context"] = {"includeDeclaration": True}
            values = self._request(method, params)
            if values is None:
                values = []
            elif isinstance(values, dict):
                values = [values]
            if not isinstance(values, list):
                raise LspProtocolError("invalid LSP locations response")
            self._ensure_current()
            results = [self._location(item) for item in values[:MAX_RESULTS]]
            return self._result(
                {"locations": results}, "locations", already_truncated=len(values) > MAX_RESULTS
            )
        except (LspProtocolError, KeyboardInterrupt):
            self.close(force=True)
            raise

    def diagnostics(self, path: str) -> str:
        """仅返回当前磁盘内容对应的诊断，未就绪或版本不明不能报告零错误。"""
        self._read(path)
        try:
            self._start()
            uri, document = self._synchronize(path)
            if isinstance(self._capabilities.get("diagnosticProvider"), dict):
                result = self._request("textDocument/diagnostic", {"textDocument": {"uri": uri}})
                if (
                    not isinstance(result, dict)
                    or result.get("kind") != "full"
                    or not isinstance(result.get("items"), list)
                ):
                    raise LspProtocolError(
                        "pull diagnostics must return a full report for this request"
                    )
                items = result["items"]
                report = Diagnostics(
                    document.version, tuple(items[:MAX_RESULTS]), len(items) > MAX_RESULTS
                )
            else:
                deadline = time.monotonic() + self._timeout
                while True:
                    report = self._diagnostics.get(uri)
                    if report is not None and report.version == document.version:
                        break
                    if report is not None and report.version is None:
                        raise LspError(
                            "diagnostics have no document version; freshness cannot be confirmed"
                        )
                    assert self._transport is not None
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise LspProtocolError("LSP diagnostics timed out")
                    message = self._transport.receive(timeout=remaining)
                    if "method" not in message:
                        raise LspProtocolError("unexpected response while waiting for diagnostics")
                    self._dispatch(message, timeout=max(0.001, deadline - time.monotonic()))
            self._ensure_current()
            converted = []
            message_truncated = False
            for item in report.items:
                if not isinstance(item, dict) or not isinstance(item.get("message"), str):
                    raise LspProtocolError("invalid diagnostic item")
                severity, code = item.get("severity"), item.get("code")
                if severity is not None and (
                    type(severity) is not int or severity not in {1, 2, 3, 4}
                ):
                    raise LspProtocolError("invalid diagnostic severity")
                if code is not None and type(code) not in {int, str}:
                    raise LspProtocolError("invalid diagnostic code")
                message_truncated |= len(item["message"]) > 4000
                converted.append(
                    {
                        "range": convert_range(document.text, item.get("range"), self._encoding),
                        "message": item["message"][:4000],
                        "severity": item.get("severity"),
                        "code": item.get("code"),
                    }
                )
            return self._result(
                {
                    "path": self.workspace.relative(self._uri_path(uri)),
                    "version": document.version,
                    "content_hash": fingerprint(document.text),
                    "fresh": True,
                    "diagnostics": converted,
                },
                "diagnostics",
                already_truncated=report.truncated or message_truncated,
            )
        except (LspProtocolError, KeyboardInterrupt):
            self.close(force=True)
            raise

    def close(self, *, force: bool = False) -> None:
        """正常退出发送 didClose/shutdown/exit，失败或取消时限时杀整个进程组。"""
        transport = self._transport
        if transport is None:
            return
        try:
            if self._initialized and not force:
                try:
                    for uri in self._documents:
                        self._notify(
                            "textDocument/didClose", {"textDocument": {"uri": uri}}, timeout=0.2
                        )
                    self._request("shutdown", None, timeout=0.5)
                    self._notify("exit", {}, timeout=0.2)
                    transport.process.wait(timeout=0.5)
                except (LspError, OSError, subprocess.TimeoutExpired):
                    # 退出不是业务重试：正常关闭失败也必须清理，而非遗留常驻进程。
                    pass
        finally:
            transport.close()
            self._transport = None
            self._documents.clear()
            self._diagnostics.clear()
            self._capabilities.clear()
            self._initialized = False
            self._sync_kind = 0
