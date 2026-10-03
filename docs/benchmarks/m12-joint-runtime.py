"""M12 固定小仓库：真实 Provider 决策、LSP、MCP、审批与恢复联合验收。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

from mini_pi.agent.events import AgentEndEvent, AgentEvent
from mini_pi.auth import resolve_api_key
from mini_pi.config import ServerConfig, UserConfig
from mini_pi.interaction import (
    ApprovalRequest,
    ApprovalResponse,
    InteractionRequest,
    InteractionResponse,
)
from mini_pi.llm.deepseek_client import DeepSeekClient
from mini_pi.llm.types import AssistantMessage, ToolMessage
from mini_pi.mcp.catalog import tool_alias
from mini_pi.security import SecurityPolicy, child_environment
from mini_pi.session.jsonl import JsonlSession
from mini_pi.session.models import ControlEntry, ToolCatalogEntry
from mini_pi.session.runtime import AgentSession
from mini_pi.tools import build_default_registry
from mini_pi.workspace.workspace import Workspace


def digest(path: Path) -> str:
    """固定测试和源码摘要只用于外部验收，不相信模型的完成陈述。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def alive(pid: int) -> bool:
    """检查本次直接启动的子进程是否确实被回收。"""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def run(pyright: Path, filesystem: Path, output: Path) -> None:
    """仅显式执行时调用真实 DeepSeek，审批严格绑定固定 server/目标/测试命令。"""
    key = resolve_api_key("deepseek", env_var="DEEPSEEK_API_KEY")
    if not key:
        raise RuntimeError("DeepSeek is not configured")
    pyright, filesystem = pyright.resolve(), filesystem.resolve()
    version = subprocess.run(
        [str(pyright.with_name("index.js")), "--version"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert version == "pyright 1.1.414"
    assert (
        json.loads((filesystem.parents[1] / "package.json").read_text())["version"] == "2026.8.31"
    )
    root = Path(tempfile.mkdtemp(prefix="mini-pi-m12-joint-")).resolve()
    (root / "rounding.py").write_text(
        "def round_price(amount: float) -> int:\n    return int(amount)\n"
    )
    (root / "app.py").write_text(
        "from rounding import round_price\nresult: str = round_price(2.5)\n"
    )
    (root / "rules.txt").write_text(
        "Only nonnegative amounts. Ask the user to choose half-up or bankers rounding.\n"
    )
    (root / "test_rounding.py").write_text(
        "import unittest\nfrom rounding import round_price\nfrom app import result\n\nclass TestRounding(unittest.TestCase):\n    def test_prices(self):\n        for amount, expected in [(2.5, 3), (2.4, 2), (0.5, 1), (3.0, 3)]:\n            self.assertEqual(round_price(amount), expected)\n        self.assertEqual(result, 3)\n"
    )
    initial = {name: digest(root / name) for name in ("rounding.py", "app.py", "test_rounding.py")}
    command = f"{shlex.quote(sys.executable)} -m unittest -q"
    approvals: list[dict[str, str]] = []
    questions: list[dict[str, object]] = []
    ends: list[str] = []
    mcp_write_attempts = 0
    session: AgentSession

    def ask(request: InteractionRequest) -> InteractionResponse:
        """脚本扮演用户选择半进一，等待时验证没有源码修改或继续提交消息。"""
        before = session.path.read_bytes()
        assert all(digest(root / name) == value for name, value in initial.items())
        time.sleep(0.2)
        assert session.path.read_bytes() == before
        questions.append(
            {
                "question": request.question,
                "options": request.options,
                "wait_without_progress": True,
            }
        )
        return InteractionResponse("采用四舍五入 half-up：仅处理非负金额，0.5 进一。")

    def approve(request: ApprovalRequest) -> ApprovalResponse:
        """只批准明确的两个进程、固定 MCP 文件写入和固定 unittest 命令。"""
        nonlocal mcp_write_attempts
        parameters = json.loads(request.parameters)
        decision = "deny"
        if request.identity.startswith("server:"):
            expected = configured.servers.get(request.target)
            assert expected is not None and parameters["argv"] == list(expected.argv)
            assert Path(request.cwd) == root
            decision = "allow"
        elif request.identity == '["mcp", "files", "write_file"]':
            assert parameters == {"path": str(root / "decision.txt"), "content": "half-up\n"}
            mcp_write_attempts += 1
            assert not (root / "decision.txt").exists()
            decision = "deny" if mcp_write_attempts == 1 else "allow"
        elif request.target == "bash":
            assert Path(request.cwd) == root and parameters["command"] == command
            decision = "allow"
        approvals.append(
            {"target": request.target, "identity": request.identity, "decision": decision}
        )
        return ApprovalResponse(decision)

    def event(event: AgentEvent) -> None:
        """只输出有用的阶段和终止原因，不打印原始工具参数或秘密。"""
        if isinstance(event, AgentEndEvent):
            ends.append(event.reason)
            print(f"agent_end: {event.reason}", flush=True)

    configured = UserConfig(
        servers={
            "python": ServerConfig(kind="lsp", argv=(str(pyright), "--stdio"), timeout=20),
            "files": ServerConfig(
                kind="mcp",
                argv=(str(filesystem), str(root)),
                tools=("read_file", "list_directory", "write_file"),
                readonly_tools=("read_file", "list_directory"),
                timeout=20,
            ),
        }
    )
    llm = DeepSeekClient(api_key=key)
    registry = build_default_registry(
        Workspace(root),
        interaction=ask,
        security=SecurityPolicy(cwd=root, config=configured, approval=approve),
    )
    session = AgentSession.create(
        cwd=root,
        provider="deepseek",
        model=llm.model,
        llm=llm,
        registry=registry,
        sessions_root=root / "sessions",
        max_steps=25,
        on_event=event,
    )
    started = time.monotonic()
    pids: list[int] = []
    try:
        task = f"""在此固定验收小仓库修复非负金额的舍入和 app.py 的类型声明，禁止修改 test_rounding.py。
先单独调用 ask_user 询问 half-up / bankers，收到回答后才修改文件。
使用 files MCP 的 read_file 读取 {root / "rules.txt"}，也调用 list_directory 查看 {root}。
使用 LSP 查 app.py 的 round_price 定义和引用，并记录 app.py 修改前后当前内容 diagnostics；修复后 app.py 和 rounding.py 均需当前诊断无错误。
按用户选择修复 rounding.py 和 app.py。通过 files MCP write_file 将 {root / "decision.txt"} 写成严格的 half-up 加换行。
该 MCP 写入第一次会被宿主拒绝，这是验收安排：被拒绝不得宣称已写入，可用相同参数再申请一次；第二次会批准。
最后通过 bash 执行严格命令 {command}，据实际结果报告。所有业务工具仍由你根据观察选择，除这个明确文件和源码修复外不做其他副作用。"""
        session.run(task)
        assert ends == ["completed"], ends
        assert len(questions) == 1 and mcp_write_attempts == 2
        assert digest(root / "test_rounding.py") == initial["test_rounding.py"]
        assert all(digest(root / name) != initial[name] for name in ("app.py", "rounding.py"))
        assert (root / "decision.txt").read_text() == "half-up\n"
        calls = {
            call.id: call
            for message in session.state.messages
            if isinstance(message, AssistantMessage)
            for call in message.tool_calls
        }
        results = [
            message for message in session.state.messages if isinstance(message, ToolMessage)
        ]
        tool_counts = Counter(calls[message.tool_call_id].name for message in results)
        definitions = [
            json.loads(message.content)
            for message in results
            if calls[message.tool_call_id].name == "lsp_definition" and not message.is_error
        ]
        assert definitions and any(
            item["path"] == "rounding.py" for item in definitions[-1]["locations"]
        )
        references = [
            json.loads(message.content)
            for message in results
            if calls[message.tool_call_id].name == "lsp_references" and not message.is_error
        ]
        assert references and {item["path"] for item in references[-1]["locations"]} >= {
            "app.py",
            "rounding.py",
        }
        for name in ("read_file", "list_directory"):
            observed = [
                json.loads(message.content)
                for message in results
                if calls[message.tool_call_id].name == tool_alias("files", name)
                and not message.is_error
            ]
            assert observed and all(
                item["source"] == {"server": "files", "tool": name} for item in observed
            )
            assert (
                any((root / "rules.txt").read_text() == item["text"] for item in observed)
                if name == "read_file"
                else any("rules.txt" in item["text"] for item in observed)
            )
        diagnostics: dict[str, list[dict]] = {}
        for message in results:
            call = calls[message.tool_call_id]
            if call.name == "lsp_diagnostics" and not message.is_error:
                diagnostics.setdefault(call.arguments["path"], []).append(
                    json.loads(message.content)
                )
        app_diagnostics = diagnostics.get("app.py") or diagnostics[str(root / "app.py")]
        assert app_diagnostics[0]["diagnostics"] and not app_diagnostics[-1]["diagnostics"]
        for path in ("app.py", "rounding.py"):
            latest = (diagnostics.get(path) or diagnostics[str(root / path)])[-1]
            assert (
                latest["fresh"]
                and latest["content_hash"] == digest(root / path)
                and not latest["diagnostics"]
            )
        assert any(
            calls[item.tool_call_id].name == "bash" and "exit_code: 0" in item.content
            for item in results
        )
        external = subprocess.run(
            [sys.executable, "-m", "unittest", "-q"],
            cwd=root,
            env=child_environment(),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert external.returncode == 0, external.stderr
        loaded = JsonlSession.load(session.path)
        assert loaded.replay().messages == tuple(session.state.messages)
        controls = [item for item in loaded.entries if isinstance(item, ControlEntry)]
        assert any(item.decision == "deny" for item in controls) and any(
            item.decision == "allow" for item in controls
        )
        assert any(isinstance(item, ToolCatalogEntry) for item in loaded.entries)
        assert key not in session.path.read_text()
        usage = session.last_run_usage
        assert usage is not None and usage.input_tokens is not None
        pids = [session._registry._lsp.pid, session._registry._mcp.clients["files"].pid]
        assert all(pid is not None for pid in pids)
        durable = session.path.read_bytes()
        session.close()
        assert not any(alive(pid) for pid in pids)
        restored = AgentSession.resume(
            session.path,
            cwd=root,
            registry=build_default_registry(Workspace(root)),
            llm_factory=lambda *args: llm,
            on_event=event,
        )
        try:
            assert tuple(restored.state.messages) == loaded.replay().messages
            restored.run("根据已有验证结果，用一句中文总结舍入决定和测试结果。不要调用任何工具。")
            assert ends[-1] == "completed" and session.path.read_bytes().startswith(durable)
            replay = JsonlSession.load(session.path)
            assert sum(
                len(item.message.tool_calls)
                for item in replay.entries
                if getattr(item, "type", None) == "message"
                and isinstance(item.message, AssistantMessage)
            ) == len(calls)
            assert restored.mcp_status == "MCP disabled" and not any(alive(pid) for pid in pids)
        finally:
            restored.close()
        record = {
            "provider": "deepseek",
            "model": llm.model,
            "workspace": str(root),
            "session": str(session.path),
            "pyright": version,
            "filesystem": "2026.8.31",
            "duration_seconds": round(time.monotonic() - started, 2),
            "tool_counts": dict(tool_counts),
            "usage": {
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "requests": usage.requests,
            },
            "questions": questions,
            "approvals": approvals,
            "checks": {
                "completed": True,
                "wait_without_progress": True,
                "mcp_deny_then_allow": True,
                "definition_references": True,
                "current_diagnostics": True,
                "test_unchanged": True,
                "external_unittest": True,
                "strict_replay": True,
                "resume_without_replay": True,
                "processes_reaped": True,
                "no_provider_key_in_jsonl": True,
            },
        }
        output.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(record["checks"]), flush=True)
    finally:
        session.close()
        llm._client.close()


def main() -> None:
    """要求人工提供已安装的两个固定版本 server 和证据输出位置。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--pyright", type=Path, required=True)
    parser.add_argument("--filesystem", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run(args.pyright, args.filesystem, args.output)


if __name__ == "__main__":
    main()
