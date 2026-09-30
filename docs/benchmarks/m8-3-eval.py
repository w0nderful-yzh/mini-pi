"""M8.3 长任务对照：区分区域估算修正和近期内容软目标的影响。"""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))

from mini_pi.agent.events import (  # noqa: E402
    AgentEndEvent,
    AgentEvent,
    MessageEndEvent,
    ToolExecutionEndEvent,
)
from mini_pi.auth import resolve_api_key  # noqa: E402
from mini_pi.context import compaction, cost  # noqa: E402
from mini_pi.context.projection import SUMMARY_TAG  # noqa: E402
from mini_pi.context.request import estimate_request  # noqa: E402
from mini_pi.context.tokens import estimate_tokens  # noqa: E402
from mini_pi.llm.deepseek_client import DeepSeekClient  # noqa: E402
from mini_pi.llm.types import (  # noqa: E402
    AssistantMessage,
    DoneEvent,
    ErrorEvent,
    Message,
    StreamEvent,
    ToolMessage,
    ToolSchema,
    UserMessage,
)
from mini_pi.session import runtime as runtime_module  # noqa: E402
from mini_pi.session.jsonl import JsonlSession  # noqa: E402
from mini_pi.session.models import CompactionEntry  # noqa: E402
from mini_pi.session.runtime import AgentSession  # noqa: E402
from mini_pi.tools import build_default_registry  # noqa: E402
from mini_pi.workspace.workspace import Workspace  # noqa: E402

VARIANTS = ("legacy", "content20k", "content8k")


def _driver() -> ModuleType:
    """复用固定仓库导出、文件指纹与外部判定器。"""
    path = Path(__file__).with_name("m8-0-eval.py")
    spec = importlib.util.spec_from_file_location("m8_3_base_driver", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


DRIVER = _driver()
TASKS = (
    DRIVER.TASKS[-1],
    DRIVER.TaskSpec(
        "multi_repo",
        "CLI 数字展示失去千位分隔，任务累计输入 Token 统计也不正确。"
        "定位并修复两处实现，运行相关测试验证；不要改测试。",
        None,
        frozenset({"mini_pi/cli/style.py", "mini_pi/cli/console.py"}),
        (sys.executable, "-m", "pytest", "-q", "tests/cli/test_visual_layout.py",
         "tests/cli/test_usage_status.py"),
    ),
)


def prepare(task: Any, destination: Path) -> None:
    """从相同固定仓库初态构造单文件或多文件修复任务。"""
    DRIVER.prepare(task, destination)
    if task.name == "multi_repo":
        path = destination / "mini_pi" / "cli" / "console.py"
        text = path.read_text(encoding="utf-8")
        old = "self._input_tokens += usage.input_tokens"
        if text.count(old) != 1:
            raise RuntimeError("multi_repo fixture no longer matches the pinned commit")
        path.write_text(text.replace(old, "self._input_tokens += usage.output_tokens"))


def _paired(messages: list[Message]) -> bool:
    """检查每个工具调用批次都有完整且唯一的紧随结果。"""
    pending: set[str] = set()
    for message in messages:
        if isinstance(message, ToolMessage):
            if message.tool_call_id not in pending:
                return False
            pending.remove(message.tool_call_id)
        else:
            if pending:
                return False
            if isinstance(message, AssistantMessage):
                pending = {call.id for call in message.tool_calls}
                if len(pending) != len(message.tool_calls):
                    return False
    return not pending


class AuditedDeepSeek(DeepSeekClient):
    """仅在评测中记录每次任务和摘要请求的估算、实测用量与配对。"""

    def __init__(self, key: str) -> None:
        """沿用 Provider 默认参数，不把凭据写入指标。"""
        super().__init__(model=DRIVER.MODEL, api_key=key)
        self.records: list[dict[str, Any]] = []

    def stream(
        self, messages: list[Message], tools: list[ToolSchema] | None = None
    ) -> Iterator[StreamEvent]:
        """一次 stream 只记一条，complete 复用它以免摘要成本重复或遗漏。"""
        record: dict[str, Any] = {
            "kind": "summary" if tools is None else "task",
            "predicted_input": estimate_request(
                messages, tools or (), provider=DRIVER.PROVIDER, model=DRIVER.MODEL
            ).input_tokens,
            "input_tokens": None, "output_tokens": None, "stop_reason": None,
            "paired": _paired(messages),
            "after_compaction": any(
                isinstance(message, UserMessage) and SUMMARY_TAG in message.content
                for message in messages
            ),
        }
        self.records.append(record)
        for event in super().stream(messages, tools):
            if isinstance(event, DoneEvent):
                usage = event.message.usage
                record.update(
                    input_tokens=usage.input_tokens if usage is not None else None,
                    output_tokens=usage.output_tokens if usage is not None else None,
                    stop_reason=event.message.stop_reason,
                )
            elif isinstance(event, ErrorEvent):
                record["stop_reason"] = "error"
            yield event


def _legacy_message_tokens(message: Message) -> int:
    """准确还原旧切点中单条 assistant 采用整次历史 usage 的计算。"""
    return estimate_tokens([message]).tokens


@contextmanager
def variant_settings(variant: str) -> Iterator[None]:
    """只改变区域估算和成本保留目标；硬窗口、事务与调用预算沿用原值。"""
    if variant not in VARIANTS:
        raise ValueError(f"unknown variant: {variant}")
    keep = 8_000 if variant == "content8k" else 20_000
    with patch.object(runtime_module, "COST_KEEP_RECENT_TOKENS", keep):
        if variant == "legacy":
            with (
                patch.object(compaction, "estimate_message_tokens", _legacy_message_tokens),
                patch.object(cost, "estimate_content_tokens", estimate_tokens),
            ):
                yield
        else:
            yield


def run_task(task: Any, *, repeat: int, variant: str, key: str) -> dict[str, Any]:
    """跑真实修复并计入所有摘要开销，随后验证源码、恢复和原始 JSONL。"""
    with tempfile.TemporaryDirectory(prefix="m8-3-") as raw_root:
        root = Path(raw_root)
        workspace = root / "project"
        prepare(task, workspace)
        before = DRIVER._fingerprints(workspace)
        initial = DRIVER._score(task, workspace, before)
        if initial["scorer_exit"] == 0:
            raise RuntimeError("initial scorer unexpectedly passes")
        client = AuditedDeepSeek(key)
        reasons: list[str] = []
        prefix = b""
        append_only = True

        def observe(event: AgentEvent) -> None:
            """只在已提交消息边界检查旧字节保留，不把审计数据写进会话。"""
            nonlocal prefix, append_only
            if isinstance(event, (MessageEndEvent, ToolExecutionEndEvent, AgentEndEvent)):
                current = runtime.path.read_bytes()
                append_only = append_only and current.startswith(prefix)
                prefix = current
            if isinstance(event, AgentEndEvent):
                reasons.append(event.reason)

        registry = build_default_registry(Workspace(workspace))
        with variant_settings(variant):
            runtime = AgentSession.create(
                cwd=workspace, llm=client, registry=registry,
                provider=DRIVER.PROVIDER, model=DRIVER.MODEL,
                sessions_root=root / "sessions", max_steps=24, on_event=observe,
            )
            started = time.perf_counter()
            runtime.run(task.prompt)
            seconds = round(time.perf_counter() - started, 2)
            restored = AgentSession.resume(
                runtime.path, registry=registry, cwd=workspace,
                llm_factory=lambda provider, model: client, max_steps=24,
            )
            resume_equal = (
                restored.state.messages == runtime.state.messages
                and restored.state.modified_files == runtime.state.modified_files
                and restored.request_snapshot() == runtime.request_snapshot()
            )
        entries = JsonlSession.load(runtime.path).active_entries()
        checkpoints = [entry for entry in entries if isinstance(entry, CompactionEntry)]
        result = DRIVER._score(task, workspace, before)
        end_reason = reasons[-1] if reasons else "unknown"
        records = client.records
        measured = [record for record in records if record["input_tokens"] is not None]
        paired = all(record["paired"] for record in records) and _paired(runtime.state.messages)
        return {
            "task": task.name, "repeat": repeat, "variant": variant,
            "fixture_id": DRIVER._fixture_identity(task, before),
            "base_commit": DRIVER.REAL_REPO_COMMIT,
            "provider": DRIVER.PROVIDER, "model": DRIVER.MODEL, "max_steps": 24,
            "initial_scorer_exit": initial["scorer_exit"], "end_reason": end_reason,
            "requests": len(records), "measured_requests": len(measured),
            "input_tokens": sum(record["input_tokens"] for record in measured),
            "output_tokens": sum(record["output_tokens"] for record in measured),
            "summary_requests": sum(record["kind"] == "summary" for record in records),
            "summary_input": sum(r["input_tokens"] for r in measured if r["kind"] == "summary"),
            "summary_output": sum(r["output_tokens"] for r in measured if r["kind"] == "summary"),
            "compactions": len(checkpoints), "request_trace": records,
            "seconds": seconds, "append_only": append_only, "paired": paired,
            "resume_equal": resume_equal, "tools": DRIVER._tool_counts(entries),
            **DRIVER._tool_observations(entries), **result,
            "success": DRIVER._task_success(end_reason, external_pass=result["external_pass"])
            and append_only and paired and resume_equal,
        }


def main() -> None:
    """每条长任务轮换三种策略两次，保留全量指标而非只保留成功样本。"""
    parser = argparse.ArgumentParser(description="M8.3 long context evaluation")
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--only", choices=[task.name for task in TASKS])
    parser.add_argument("--jsonl", type=Path, required=True)
    args = parser.parse_args()
    if args.repeats <= 0:
        parser.error("--repeats must be positive")
    key = resolve_api_key(DRIVER.PROVIDER, env_var="DEEPSEEK_API_KEY")
    if not key:
        raise SystemExit("DeepSeek API Key 不可用：真实对照未执行")
    commit = subprocess.run(
        ["git", "-C", str(PROJECT), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    with args.jsonl.open("w", encoding="utf-8") as output:
        for task in TASKS:
            if args.only and args.only != task.name:
                continue
            for repeat in range(1, args.repeats + 1):
                order = VARIANTS if repeat % 2 else tuple(reversed(VARIANTS))
                identity: str | None = None
                for variant in order:
                    print(f"running {task.name} {repeat} {variant}", file=sys.stderr, flush=True)
                    record = run_task(task, repeat=repeat, variant=variant, key=key)
                    if identity is not None and record["fixture_id"] != identity:
                        raise RuntimeError("paired variants used different fixtures")
                    identity = record["fixture_id"]
                    record["runtime_base_commit"] = commit
                    line = json.dumps(record, ensure_ascii=False, sort_keys=True)
                    output.write(line + "\n")
                    output.flush()
                    print(line, flush=True)


if __name__ == "__main__":
    main()
