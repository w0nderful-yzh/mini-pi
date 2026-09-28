"""M7.9.3 基线与规模测量驱动器：真实任务基线、Session 规模、工具请求开销。

用法（在仓库根执行，`real` / `tools` 需要已配置的 DeepSeek Key）：

    uv run python docs/benchmarks/m7-9-baseline.py scale
    uv run python docs/benchmarks/m7-9-baseline.py tools
    uv run python docs/benchmarks/m7-9-baseline.py real --max-steps 12
    uv run python docs/benchmarks/m7-9-baseline.py all --json /tmp/m7-9.json

`real` 在临时 workspace 与临时 HOME 中运行，不触碰仓库文件、真实 Session 与输入历史；
只打印计数、耗时、内存与 Provider 实测用量，不打印 API Key 或模型回复原文。
`scale` 与 `tools` 之外的模式不需要网络。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import tracemalloc
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROJECT = Path(__file__).resolve().parents[2]
FIXTURE_PROJECT = PROJECT / "tests" / "fixtures" / "sample_project"
sys.path.insert(0, str(PROJECT))

from mini_pi.auth import resolve_api_key  # noqa: E402
from mini_pi.cli.app import EXIT_CODES  # noqa: E402
from mini_pi.context.request import estimate_request  # noqa: E402
from mini_pi.errors import SessionError  # noqa: E402
from mini_pi.llm.deepseek_client import DeepSeekClient  # noqa: E402
from mini_pi.llm.types import (  # noqa: E402
    AssistantMessage,
    Message,
    StreamEvent,
    SystemMessage,
    ToolMessage,
    ToolSchema,
    UserMessage,
)
from mini_pi.session.jsonl import (  # noqa: E402
    JsonlSession,
    discover_session_files,
    latest_session_path,
    list_session_summaries,
)
from mini_pi.session.runtime import AgentSession  # noqa: E402
from mini_pi.session.usage import recent_session_run_usage  # noqa: E402
from mini_pi.tools import build_default_registry  # noqa: E402
from mini_pi.tools.registry import ToolRegistry  # noqa: E402
from mini_pi.workspace.workspace import Workspace  # noqa: E402

MODEL = "deepseek-flash"
PROVIDER = "deepseek"
_TASK_TIMEOUT_S = 600

# 与离线基线（tests/integration/test_task_baseline.py）同一组任务形态
TASK_PROMPTS: tuple[tuple[str, str], ...] = (
    ("read_only", "calculator.py 里的 add 做了什么？"),
    ("locate", "找出 add 的定义位置和调用位置。"),
    ("modify_and_verify", "运行 pytest，修复失败用例，然后再次运行 pytest 验证。"),
    ("recover_from_failure", "运行 pytest 并修复失败用例。"),
)


class UnusedLLM:
    """占位客户端：规模测量只加载 Session，任何模型调用都属于测量错误。"""

    def stream(
        self, messages: list[Message], tools: list[ToolSchema] | None = None
    ) -> Iterator[StreamEvent]:
        """契约：规模测量不得请求模型。"""
        raise AssertionError("scale measurement must not call the model")

    def complete(
        self, messages: list[Message], tools: list[ToolSchema] | None = None
    ) -> AssistantMessage:
        """契约：规模测量不得请求模型。"""
        raise AssertionError("scale measurement must not call the model")


@dataclass(frozen=True, slots=True)
class Measurement:
    """一次测量的耗时与 Python 分配峰值。"""

    seconds: float
    peak_mb: float


def measure(action: Callable[[], Any]) -> Measurement:
    """执行一次加载动作并记录耗时与 tracemalloc 峰值。"""
    tracemalloc.start()
    started = time.perf_counter()
    action()
    seconds = time.perf_counter() - started
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    return Measurement(seconds=seconds, peak_mb=peak / 1024 / 1024)


def copy_sample_project(destination: Path) -> Path:
    """复制样例项目作为真实任务的隔离 workspace。"""
    shutil.copytree(
        FIXTURE_PROJECT,
        destination,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    return destination


def pytest_passes(workspace: Path) -> bool:
    """在任务结束后直接跑 pytest，用真实结果而不是模型总结判断任务是否完成。"""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=workspace,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def reason_for_exit_code(code: int) -> str:
    """把一次性进程退出码映射回终止原因（M7.9.1 的退出码契约）。"""
    for reason, value in EXIT_CODES.items():
        if value == code:
            return reason
    return f"unknown({code})"


def run_real_task(
    name: str,
    prompt: str,
    *,
    root: Path,
    api_key: str,
    max_steps: int,
) -> dict[str, Any]:
    """在隔离 workspace 与 HOME 中跑一条真实任务，返回可记录的指标。"""
    workspace = copy_sample_project(root / "project")
    home = root / "home"
    home.mkdir()
    sessions_root = home / ".mini-pi" / "sessions"
    env = {
        **os.environ,
        "HOME": str(home),
        "DEEPSEEK_API_KEY": api_key,
        "NO_COLOR": "1",
    }
    argv = [
        sys.executable,
        "-c",
        "from mini_pi.cli.app import main; main()",
        prompt,
        "--provider",
        PROVIDER,
        "--model",
        MODEL,
        "--cwd",
        str(workspace),
        "--no-banner",
        "--max-steps",
        str(max_steps),
    ]
    started = time.perf_counter()
    try:
        result = subprocess.run(
            argv,
            cwd=PROJECT,
            env=env,
            capture_output=True,
            text=True,
            timeout=_TASK_TIMEOUT_S,
        )
        exit_code: int | None = result.returncode
        timed_out = False
    except subprocess.TimeoutExpired:
        # 超时属于要记录的结果，不能丢掉同一轮的其他任务记录
        exit_code = None
        timed_out = True
    seconds = time.perf_counter() - started

    files = discover_session_files(workspace, sessions_root=sessions_root)
    session = JsonlSession.load(files[0]) if files else None
    usage = recent_session_run_usage(session.active_entries()) if session else None
    tools: Counter[str] = Counter()
    if session is not None:
        tools = Counter(
            entry.message.name
            for entry in session.active_entries()
            if isinstance(entry.message, ToolMessage)
        )
    return {
        "task": name,
        "exit_code": exit_code,
        "end_reason": "timeout" if timed_out else reason_for_exit_code(int(exit_code)),
        "tests_pass": pytest_passes(workspace),
        "seconds": round(seconds, 1),
        "requests": usage.requests if usage else None,
        "measured_requests": usage.measured_requests if usage else None,
        "tool_calls": usage.tool_calls if usage else None,
        "tools": dict(sorted(tools.items())),
        "input_tokens": usage.input_tokens if usage else None,
        "output_tokens": usage.output_tokens if usage else None,
        "latest_input_tokens": usage.latest_input_tokens if usage else None,
    }


def mode_real(args: argparse.Namespace, records: list[dict[str, Any]]) -> None:
    """四条固定任务各跑一次真实请求，并打印任务级指标。"""
    key = resolve_api_key(PROVIDER, env_var="DEEPSEEK_API_KEY")
    if not key:
        raise SystemExit("DeepSeek API Key 不可用：真实任务基线未执行，不声称通过")
    selected = [
        item for item in TASK_PROMPTS if not args.only or item[0] in set(args.only)
    ]
    if not selected:
        raise SystemExit(f"没有匹配的任务：{args.only}")
    for name, prompt in selected:
        with tempfile.TemporaryDirectory(prefix=f"m7-9-{name}-") as raw_root:
            record = run_real_task(
                name,
                prompt,
                root=Path(raw_root),
                api_key=key,
                max_steps=args.max_steps,
            )
        records.append(record)
        print(json.dumps(record, ensure_ascii=False), flush=True)


def build_session_file(
    sessions_root: Path,
    workspace: Path,
    *,
    messages: int,
    tag: str,
) -> Path:
    """生成一个只含 user/assistant 的合法 Session；tool 配对不在规模测量范围内。"""
    session = JsonlSession.create(
        cwd=workspace,
        provider=PROVIDER,
        model=MODEL,
        sessions_root=sessions_root,
    )
    for index in range(messages):
        is_user = index % 2 == 0
        message: Message = (
            UserMessage(content=f"{tag} task {index}: 请检查第 {index} 行的实现。")
            if is_user
            else AssistantMessage(content=f"{tag} answer {index}: 第 {index} 行没有问题。")
        )
        session.append_message(
            message,
            provider=PROVIDER,
            model=MODEL,
            step_count=index // 2 + 1,
        )
    return session.path


def mode_scale(args: argparse.Namespace, records: list[dict[str, Any]]) -> None:
    """测量长 JSONL 的恢复耗时/内存与多候选列表的加载耗时/内存。"""
    for messages in (100, 400, 1600):
        with tempfile.TemporaryDirectory(prefix="m7-9-scale-") as raw_root:
            root = Path(raw_root)
            workspace = root / "project"
            workspace.mkdir()
            sessions_root = root / "sessions"
            path = build_session_file(
                sessions_root, workspace, messages=messages, tag="long"
            )
            size_kb = path.stat().st_size / 1024
            resume = measure(
                lambda: AgentSession.resume(
                    path,
                    registry=ToolRegistry(),
                    cwd=workspace,
                    llm_factory=lambda provider, model: UnusedLLM(),
                )
            )
            record = {
                "kind": "resume",
                "messages": messages,
                "candidates": 1,
                "jsonl_kb": round(size_kb, 1),
                "seconds": round(resume.seconds, 4),
                "peak_mb": round(resume.peak_mb, 2),
            }
        records.append(record)
        print(json.dumps(record, ensure_ascii=False), flush=True)

    for candidates in (10, 40):
        with tempfile.TemporaryDirectory(prefix="m7-9-candidates-") as raw_root:
            root = Path(raw_root)
            workspace = root / "project"
            workspace.mkdir()
            sessions_root = root / "sessions"
            session_messages = 200
            for index in range(candidates):
                build_session_file(
                    sessions_root, workspace, messages=session_messages, tag=f"c{index}"
                )
            listing = measure(
                lambda: list_session_summaries(workspace, sessions_root=sessions_root)
            )
            record: dict[str, Any] = {
                "kind": "list",
                "messages": session_messages,
                "candidates": candidates,
                "seconds": round(listing.seconds, 4),
                "peak_mb": round(listing.peak_mb, 2),
            }
            try:
                latest = measure(
                    lambda: latest_session_path(workspace, sessions_root=sessions_root)
                )
                record["latest_seconds"] = round(latest.seconds, 4)
                record["latest_peak_mb"] = round(latest.peak_mb, 2)
            except SessionError as exc:
                # 活动时间并列时 --continue 会明确失败；这是要记录的结论，不是脚本故障
                record["latest_error"] = str(exc)
        records.append(record)
        print(json.dumps(record, ensure_ascii=False), flush=True)


def mode_tools(args: argparse.Namespace, records: list[dict[str, Any]]) -> None:
    """复核当前工具集下的「工具模式」请求开销：估算 vs Provider 实测 input。"""
    key = resolve_api_key(PROVIDER, env_var="DEEPSEEK_API_KEY")
    if not key:
        raise SystemExit("DeepSeek API Key 不可用：工具开销复核未执行，不声称通过")
    client = DeepSeekClient(model=MODEL, api_key=key)
    schemas = build_default_registry(Workspace(PROJECT)).schemas()
    messages = [
        SystemMessage(content="请只回复：收到。"),
        UserMessage(content="请回复收到。"),
    ]
    for label, tools in (("no_tools", []), (f"default_registry_{len(schemas)}", schemas)):
        prediction = estimate_request(messages, tools, provider=PROVIDER, model=MODEL)
        response = client.complete(messages, tools)
        if response.usage is None or response.usage.input_tokens <= 0:
            raise SystemExit(f"{label}: Provider 未返回有效 input usage")
        actual = response.usage.input_tokens
        record = {
            "kind": "tool_overhead",
            "case": label,
            "tools": len(tools),
            "estimate": prediction.input_tokens,
            "input_usage": actual,
            "error_pct": round((prediction.input_tokens / actual - 1) * 100, 1),
        }
        records.append(record)
        print(json.dumps(record, ensure_ascii=False), flush=True)


def main() -> None:
    """解析模式并依次执行；`--json` 落盘全部记录供验收文档引用。"""
    parser = argparse.ArgumentParser(description="M7.9.3 baseline and scale measurements")
    parser.add_argument(
        "mode",
        choices=("real", "scale", "tools", "all"),
        help="real / tools 需要网络与 Key；scale 完全离线",
    )
    parser.add_argument("--max-steps", type=int, default=12, help="单次真实任务的步数上限")
    parser.add_argument(
        "--only",
        action="append",
        default=None,
        help="只跑指定任务名（可重复），用于重跑单条基线",
    )
    parser.add_argument("--json", type=Path, default=None, help="把全部记录写到该文件")
    args = parser.parse_args()

    records: list[dict[str, Any]] = []
    if args.mode in ("real", "all"):
        mode_real(args, records)
    if args.mode in ("scale", "all"):
        mode_scale(args, records)
    if args.mode in ("tools", "all"):
        mode_tools(args, records)
    if args.json is not None:
        args.json.write_text(
            json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"wrote {len(records)} records to {args.json}")


if __name__ == "__main__":
    main()
