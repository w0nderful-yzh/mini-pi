"""真实管道验证分帧失败、资源边界和进程组清理。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from mini_pi.lsp.transport import MAX_STDERR_BYTES, LspProtocolError, StdioTransport


def spawn(code: str, *args: str) -> subprocess.Popen[bytes]:
    """测试子进程使用独立组和真实三管道，与受控 server 启动方式一致。"""
    return subprocess.Popen(
        [sys.executable, "-c", code, *args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )


@pytest.mark.parametrize(
    "frame",
    [
        b"Content-Length: 1\r\nContent-Length: 1\r\n\r\n0",
        b"Content-Length: -1\r\n\r\n",
        b"Content-Length: 2000001\r\n\r\n",
        b"Content-Length: 1\r\nContent-Type: application/vscode-jsonrpc; charset=latin1\r\n\r\n0",
        b"Content-Length: 1\r\n\r\n{",
        b"Content-Length: 9\r\n\r\n{}",
        b"x" * 8193,
        b"",
    ],
)
def test_malformed_partial_and_eof_fail_bounded(frame: bytes) -> None:
    """非法头、正文、截断和 EOF 均转明确协议错误，没有无界等待。"""
    transport = StdioTransport(
        spawn(f"import sys; sys.stdout.buffer.write({frame!r}); sys.stdout.buffer.flush()")
    )
    try:
        with pytest.raises(LspProtocolError):
            transport.receive(timeout=1)
    finally:
        transport.close()
    assert transport.process.poll() is not None


def test_unicode_byte_length_and_multiple_frames() -> None:
    """同一次管道写入的多帧按 UTF-8 字节长度分割，正文 Unicode 不损坏。"""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": "中文😀"}, ensure_ascii=False).encode()
    frame = f"Content-Length: {len(body)}\r\n\r\n".encode() + body
    transport = StdioTransport(
        spawn(f"import sys; sys.stdout.buffer.write({frame + frame!r}); sys.stdout.buffer.flush()")
    )
    try:
        assert transport.receive(timeout=1)["result"] == "中文😀"
        assert transport.receive(timeout=1)["result"] == "中文😀"
    finally:
        transport.close()


def test_stalled_write_times_out() -> None:
    """server 不读 stdin 时，大请求仍在时限内终止并可回收。"""
    transport = StdioTransport(spawn("import time; time.sleep(30)"))
    try:
        start = time.monotonic()
        with pytest.raises(LspProtocolError, match="write timed out"):
            transport.send(
                {"jsonrpc": "2.0", "method": "test", "params": "x" * 1_000_000}, timeout=0.1
            )
        assert time.monotonic() - start < 2
    finally:
        transport.close()


def test_stderr_is_drained_bounded_and_redacted() -> None:
    """超过管道容量的错误日志不死锁，捕获尾部有界且凭据不回流。"""
    transport = StdioTransport(
        spawn("import sys; sys.stderr.write('x' * 200000 + 'private-token'); sys.stderr.flush()"),
        ("private-token",),
    )
    try:
        transport.process.wait(timeout=2)
        transport._stderr_thread.join(timeout=1)
        with pytest.raises(LspProtocolError) as error:
            transport.receive(timeout=1)
        assert "private-token" not in str(error.value) and "[REDACTED]" in str(error.value)
        assert len(transport._stderr) <= MAX_STDERR_BYTES
    finally:
        transport.close()


def test_close_kills_descendant_holding_pipes(tmp_path: Path) -> None:
    """进程组中继承管道的孙进程也被终止，不遗留 stderr 捕获线程。"""
    marker = tmp_path / "child.pid"
    transport = StdioTransport(
        spawn(
            "import subprocess,sys,time; from pathlib import Path; "
            "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
            "Path(sys.argv[1]).write_text(str(child.pid)); time.sleep(30)",
            str(marker),
        )
    )
    try:
        deadline = time.monotonic() + 2
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        child_pid = int(marker.read_text())
    finally:
        transport.close()
    assert not transport._stderr_thread.is_alive()
    status = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(child_pid)], capture_output=True, text=True
    ).stdout.strip()
    assert not status or status.startswith("Z")
    with pytest.raises(ProcessLookupError):
        os.kill(transport.process.pid, 0)


def test_partial_transport_construction_cleans_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """初始化管道失败仍回收刚启动的进程，异常按程序缺陷冒泡。"""
    process = spawn("import time; time.sleep(30)")

    def fail(*args: object) -> None:
        """模拟宿主无法设置管道属性，不伪造协议错误。"""
        raise OSError("cannot configure pipe")

    monkeypatch.setattr(os, "set_blocking", fail)
    with pytest.raises(OSError, match="configure pipe"):
        StdioTransport(process)
    assert process.poll() is not None
