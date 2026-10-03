"""离线 LSP stdio server：制造协议、同步和生命周期边界，不调用网络。"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

mode = sys.argv[1]
events = Path(sys.argv[2])
documents: dict[str, dict[str, Any]] = {}
encoding = {"utf8": "utf-8", "utf32": "utf-32"}.get(mode, "utf-16")


def record(value: dict[str, Any]) -> None:
    """将真实收到的帧写入测试临时目录，供宿主检查顺序与参数。"""
    with events.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False) + "\n")


def receive() -> dict[str, Any] | None:
    """按 Content-Length 读取真实客户端帧。"""
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line == b"\r\n":
            break
        name, _, value = line.decode("ascii").partition(":")
        headers[name.lower()] = value.strip()
    size = int(headers["content-length"])
    result = json.loads(sys.stdin.buffer.read(size))
    record(result)
    return result


def send(value: dict[str, Any]) -> None:
    """故意拆开头部与 Unicode 内容，验证客户端正确组合分片。"""
    body = json.dumps({"jsonrpc": "2.0", **value}, ensure_ascii=False).encode()
    frame = f"Content-Length: {len(body)}\r\n\r\n".encode() + body
    for chunk in (frame[:8], frame[8:31], frame[31:]):
        sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()


def diagnostic(uri: str, *, old: bool = False) -> None:
    """只对打开文件发布版本诊断；模式可制造过期或版本缺失。"""
    document = documents[uri]
    version = document["version"] - 1 if old else document["version"]
    bad = "bad" in document["text"] or (
        mode == "dependency" and any("bad" in item["text"] for item in documents.values())
    )
    items = (
        [
            {
                "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}},
                "message": "bad type",
                "severity": 1,
                "code": "test",
            }
        ]
        if bad
        else []
    )
    if mode == "large_diagnostics":
        items = [
            {
                "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}},
                "message": "中文😀" * 1500,
                "severity": 1,
            }
        ] * 100
    if mode == "secret":
        items = [
            {
                "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}},
                "message": os.environ["TEST_TOKEN"],
                "severity": 1,
            }
        ]
    params = {"uri": uri, "diagnostics": items}
    if mode != "unversioned":
        params["version"] = version
    send({"method": "textDocument/publishDiagnostics", "params": params})


def control_requests() -> None:
    """server 反向请求配置、根目录与文件编辑，等待明确响应后才继续初始化。"""
    for request_id, method, params in [
        ("config", "workspace/configuration", {"items": [{"section": "python"}]}),
        ("folders", "workspace/workspaceFolders", {}),
        ("edit", "workspace/applyEdit", {"edit": {"changes": {"file:///tmp/hacked": []}}}),
        ("unknown", "client/registerCapability", {"registrations": []}),
    ]:
        send({"id": request_id, "method": method, "params": params})
        response = receive()
        assert response is not None and response["id"] == request_id


def main() -> None:
    """顺序处理请求，用真实进程触发断连、超时和清理测试。"""
    while (message := receive()) is not None:
        method = message.get("method")
        params = message.get("params", {})
        if method == "initialize":
            if mode == "init_error":
                send({"id": message["id"], "error": {"code": -32002, "message": "init failed"}})
                continue
            if mode == "controls":
                control_requests()
            capabilities = {
                "definitionProvider": mode != "no_definitions",
                "referencesProvider": True,
                "positionEncoding": encoding,
                "textDocumentSync": {"openClose": True, "change": 1 if mode == "full_sync" else 2},
            }
            if mode == "no_sync":
                capabilities["textDocumentSync"] = 0
            if mode == "bad_encoding":
                capabilities["positionEncoding"] = ["utf-16"]
            if mode in {"pull", "unchanged_pull"}:
                capabilities["diagnosticProvider"] = {
                    "interFileDependencies": True,
                    "workspaceDiagnostics": False,
                }
            send({"id": message["id"], "result": {"capabilities": capabilities}})
        elif method == "textDocument/didOpen":
            document = params["textDocument"]
            documents[document["uri"]] = dict(document)
            if mode not in {"pull", "unchanged_pull", "silent"}:
                diagnostic(document["uri"], old=mode == "stale")
        elif method == "textDocument/didChange":
            uri = params["textDocument"]["uri"]
            documents[uri].update(
                version=params["textDocument"]["version"], text=params["contentChanges"][0]["text"]
            )
            if mode not in {"pull", "unchanged_pull", "silent"}:
                if mode == "late_diagnostics":
                    diagnostic(uri, old=True)
                diagnostic(uri, old=mode == "stale")
        elif method in {"textDocument/definition", "textDocument/references"}:
            if mode == "hang":
                time.sleep(30)
                continue
            if mode == "crash":
                os._exit(4)
            if mode == "bad_frame":
                sys.stdout.buffer.write(b"Content-Length: 2000001\r\n\r\n")
                sys.stdout.buffer.flush()
                continue
            if mode == "wrong_id":
                send({"id": message["id"] + 1, "result": []})
                continue
            if mode == "changed_during_query":
                uri = params["textDocument"]["uri"]
                from urllib.parse import unquote, urlsplit

                Path(unquote(urlsplit(uri).path)).write_text("changed = 1\n")
            location = {
                "uri": params["textDocument"]["uri"],
                "range": {"start": params["position"], "end": params["position"]},
            }
            if mode == "outside":
                location["uri"] = "file:///private/mini-pi-test-outside.py"
            if mode == "malformed_uri":
                location["uri"] = "file://[invalid"
            if mode == "link":
                location = {
                    "targetUri": location["uri"],
                    "targetRange": location["range"],
                    "targetSelectionRange": location["range"],
                }
            value = (
                [location] * (150 if mode == "many" else 2)
                if method.endswith("references")
                else location
            )
            send({"method": "window/logMessage", "params": {"type": 3, "message": "交错通知😀"}})
            send({"id": message["id"], "result": value})
        elif method == "textDocument/diagnostic":
            result = (
                {"kind": "unchanged", "resultId": "old"}
                if mode == "unchanged_pull"
                else {"kind": "full", "items": []}
            )
            send({"id": message["id"], "result": result})
        elif method == "textDocument/didClose":
            documents.pop(params["textDocument"]["uri"], None)
        elif method == "shutdown":
            if mode == "hang_shutdown":
                time.sleep(30)
            else:
                send({"id": message["id"], "result": None})
        elif method == "exit":
            return


if __name__ == "__main__":
    main()
