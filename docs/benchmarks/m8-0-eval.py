"""M8.0 固定任务评测：复制初态、运行真实 CLI、由外部命令判定并保存逐次记录。"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROJECT = Path(__file__).resolve().parents[2]
FIXTURES = PROJECT / "tests" / "fixtures" / "m8_eval"
sys.path.insert(0, str(PROJECT))

from mini_pi.auth import resolve_api_key  # noqa: E402
from mini_pi.cli.app import EXIT_CODES  # noqa: E402
from mini_pi.llm.types import ToolMessage  # noqa: E402
from mini_pi.session.jsonl import JsonlSession, discover_session_files  # noqa: E402
from mini_pi.session.models import MessageEntry  # noqa: E402
from mini_pi.session.usage import recent_session_run_tools, recent_session_run_usage  # noqa: E402

PROVIDER = "deepseek"
MODEL = "deepseek-flash"
REAL_REPO_COMMIT = "72a504c65da9cd6d4350accada0a8a6f2515d653"
REAL_BUG_OLD = 'return f"{value:,}"'
REAL_BUG_NEW = "return str(value)"
_COMMAND_TIMEOUT = 180
_TASK_TIMEOUT = 600


@dataclass(frozen=True, slots=True)
class TaskSpec:
    """固定任务的初态、提示词、允许改动和外部判定命令。"""

    name: str
    prompt: str
    fixture: str | None
    allowed_changes: frozenset[str]
    scorer: tuple[str, ...]


TASKS = (
    TaskSpec(
        "multi_file",
        "费用规则和订单合计测试失败。定位并修复两个实现文件，运行测试验证；不要改测试。",
        "multi_file",
        frozenset({"fees.py", "invoice.py"}),
        (sys.executable, "-m", "pytest", "-q"),
    ),
    TaskSpec(
        "failure_recovery",
        "运行测试，依据失败信息修复逗号分隔整数解析中的边界问题，再运行测试验证；不要改测试。",
        "recovery",
        frozenset({"parser.py"}),
        (sys.executable, "-m", "pytest", "-q"),
    ),
    TaskSpec(
        "config_ci",
        "项目代码测试本身通过，但默认 pytest 命令未发现用例。修复 pytest 配置，使默认测试命令执行 tests 下的用例；不要改测试。",
        "config_ci",
        frozenset({"pyproject.toml"}),
        (sys.executable, "-m", "pytest", "-q"),
    ),
    TaskSpec(
        "real_repo",
        "本仓库的数字展示失去千位分隔。定位并修复 CLI 格式化实现，运行相关测试验证；不要改测试。",
        None,
        frozenset({"mini_pi/cli/style.py"}),
        (sys.executable, "-m", "pytest", "-q", "tests/cli/test_visual_layout.py"),
    ),
)


def _reason(code: int | None, *, timed_out: bool) -> str:
    """由一次性 CLI 退出码还原终止原因，超时单列。"""
    if timed_out:
        return "timeout"
    for reason, value in EXIT_CODES.items():
        if value == code:
            return reason
    return f"unknown({code})"


def _copy_repo(destination: Path) -> None:
    """从固定提交导出真实仓库，并注入一处可重现的展示缺陷。"""
    archive = subprocess.run(
        ["git", "-C", str(PROJECT), "archive", "--format=tar", REAL_REPO_COMMIT],
        check=True,
        capture_output=True,
    )
    destination.mkdir()
    with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as source:
        # 输入是本地固定提交；仍用 data filter 拒绝路径逃逸和特殊文件。
        source.extractall(destination, filter="data")
    target = destination / "mini_pi" / "cli" / "style.py"
    original = target.read_text(encoding="utf-8")
    if original.count(REAL_BUG_OLD) != 1:
        raise RuntimeError("real_repo fixture no longer matches the pinned commit")
    target.write_text(original.replace(REAL_BUG_OLD, REAL_BUG_NEW), encoding="utf-8")


def prepare(spec: TaskSpec, destination: Path) -> None:
    """为每次运行构造独立且相同的故障初态。"""
    if spec.fixture is None:
        _copy_repo(destination)
    else:
        shutil.copytree(FIXTURES / spec.fixture, destination)


def _fingerprints(workspace: Path) -> dict[str, str]:
    """记录源文件指纹，忽略测试运行时产生的缓存。"""
    result: dict[str, str] = {}
    for path in workspace.rglob("*"):
        if not path.is_file() or any(
            part in {"__pycache__", ".pytest_cache", ".ruff_cache", ".git", ".venv"}
            for part in path.relative_to(workspace).parts
        ) or path.suffix == ".pyc":
            continue
        result[path.relative_to(workspace).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _score(spec: TaskSpec, workspace: Path, before: dict[str, str]) -> dict[str, Any]:
    """用工作区外部命令和文件指纹判定，拒绝篡改测试与无关文件。"""
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            spec.scorer,
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=_COMMAND_TIMEOUT,
            env=_scorer_env(),
        )
        scorer_exit: int | None = completed.returncode
    except subprocess.TimeoutExpired:
        scorer_exit = None
    after = _fingerprints(workspace)
    changed = sorted(
        path for path in before.keys() | after.keys() if before.get(path) != after.get(path)
    )
    unexpected = sorted(set(changed) - spec.allowed_changes)
    return {
        "scorer_exit": scorer_exit,
        "scorer_seconds": round(time.perf_counter() - started, 2),
        "changed_files": changed,
        "unexpected_files": unexpected,
        "external_pass": scorer_exit == 0 and not unexpected and bool(set(changed) & spec.allowed_changes),
    }


def _scorer_env() -> dict[str, str]:
    """固定终端环境，避免宿主 NO_COLOR/TERM 改变 CLI 测试断言。"""
    env = {**os.environ, "TERM": "xterm-256color", "PYTHONDONTWRITEBYTECODE": "1"}
    env.pop("NO_COLOR", None)
    return env


def _fixture_identity(spec: TaskSpec, before: dict[str, str]) -> str:
    """把初态内容、任务提示和判定命令合成可核对的版本指纹。"""
    value = json.dumps(
        {"files": before, "prompt": spec.prompt, "scorer": spec.scorer},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _tool_observations(entries: Sequence[Any]) -> dict[str, int]:
    """只计可验证的相同参数重复和测试命令，不推断它们是否多余。"""
    calls = recent_session_run_tools(entries)
    seen: set[tuple[str, str]] = set()
    repeated = 0
    verification = 0
    errors = 0
    for pair in calls:
        call = pair.call
        identity = (call.name, json.dumps(call.arguments, sort_keys=True, ensure_ascii=False))
        if identity in seen:
            repeated += 1
        seen.add(identity)
        if call.name == "bash" and re.search(
            r"(?:^|\W)(?:pytest|ruff|compileall|gradle)(?:\W|$)",
            str(call.arguments.get("command", "")),
        ):
            verification += 1
        if pair.result.is_error:
            errors += 1
    return {
        "repeated_exact_calls": repeated,
        "verification_commands": verification,
        "tool_errors": errors,
    }


def _tool_counts(entries: Sequence[Any]) -> dict[str, int]:
    """只从 message entry 计工具，压缩检查点不属于工具调用。"""
    counts = Counter(
        entry.message.name
        for entry in entries
        if isinstance(entry, MessageEntry) and isinstance(entry.message, ToolMessage)
    )
    return dict(sorted(counts.items()))


def _task_success(end_reason: str, *, external_pass: bool) -> bool:
    """模型完成且外部判定通过才算任务成功，步数/预算耗尽均不算。"""
    return end_reason == "completed" and external_pass


def run_task(spec: TaskSpec, *, repeat: int, key: str, max_steps: int) -> dict[str, Any]:
    """运行一次隔离的真实任务，并保留可归因的逐次指标。"""
    with tempfile.TemporaryDirectory(prefix=f"m8-0-{spec.name}-{repeat}-") as raw_root:
        root = Path(raw_root)
        workspace = root / "project"
        prepare(spec, workspace)
        before = _fingerprints(workspace)
        initial = subprocess.run(
            spec.scorer,
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=_COMMAND_TIMEOUT,
            env=_scorer_env(),
        )
        if initial.returncode == 0:
            raise RuntimeError(f"{spec.name}: initial scorer unexpectedly passes")
        home = root / "home"
        home.mkdir()
        env = {
            **os.environ,
            "HOME": str(home),
            "DEEPSEEK_API_KEY": key,
            "NO_COLOR": "1",
            "TERM": "dumb",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        command = (
            sys.executable,
            "-c",
            "from mini_pi.cli.app import main; main()",
            spec.prompt,
            "--provider",
            PROVIDER,
            "--model",
            MODEL,
            "--cwd",
            str(workspace),
            "--no-banner",
            "--max-steps",
            str(max_steps),
        )
        started = time.perf_counter()
        try:
            execution = subprocess.run(
                command, cwd=PROJECT, env=env, capture_output=True, text=True, timeout=_TASK_TIMEOUT
            )
            exit_code: int | None = execution.returncode
            timed_out = False
        except subprocess.TimeoutExpired:
            exit_code = None
            timed_out = True
        seconds = round(time.perf_counter() - started, 2)
        files = discover_session_files(workspace, sessions_root=home / ".mini-pi" / "sessions")
        if len(files) > 1:
            raise RuntimeError(f"{spec.name}: expected at most one session, found {len(files)}")
        session = JsonlSession.load(files[0]) if files else None
        entries = session.active_entries() if session else []
        usage = recent_session_run_usage(entries)
        tools = _tool_counts(entries)
        result = _score(spec, workspace, before)
        end_reason = _reason(exit_code, timed_out=timed_out)
        return {
            "task": spec.name,
            "repeat": repeat,
            "fixture_id": _fixture_identity(spec, before),
            "base_commit": REAL_REPO_COMMIT if spec.fixture is None else None,
            "model": MODEL,
            "provider": PROVIDER,
            "python": platform.python_version(),
            "platform": platform.platform(),
            "max_steps": max_steps,
            "initial_scorer_exit": initial.returncode,
            "exit_code": exit_code,
            "end_reason": end_reason,
            "requests": usage.requests if usage else None,
            "measured_requests": usage.measured_requests if usage else None,
            "tool_calls": usage.tool_calls if usage else None,
            "tools": tools,
            **_tool_observations(entries),
            "input_tokens": usage.input_tokens if usage else None,
            "output_tokens": usage.output_tokens if usage else None,
            "latest_input_tokens": usage.latest_input_tokens if usage else None,
            "seconds": seconds,
            **result,
            "success": _task_success(end_reason, external_pass=result["external_pass"]),
        }


def main() -> None:
    """按固定顺序重复任务，并逐次写出可恢复的 JSONL 记录。"""
    parser = argparse.ArgumentParser(description="M8.0 repeated real task evaluation")
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--max-steps", type=int, default=12)
    parser.add_argument("--only", action="append", choices=[task.name for task in TASKS])
    parser.add_argument("--jsonl", type=Path)
    args = parser.parse_args()
    if args.repeats <= 0 or args.max_steps <= 0:
        parser.error("--repeats and --max-steps must be positive")
    key = resolve_api_key(PROVIDER, env_var="DEEPSEEK_API_KEY")
    if not key:
        raise SystemExit("DeepSeek API Key 不可用：真实对拍未执行")
    selected = [task for task in TASKS if not args.only or task.name in args.only]
    output = args.jsonl.open("w", encoding="utf-8") if args.jsonl else None
    try:
        for spec in selected:
            for repeat in range(1, args.repeats + 1):
                record = run_task(spec, repeat=repeat, key=key, max_steps=args.max_steps)
                line = json.dumps(record, ensure_ascii=False, sort_keys=True)
                print(line, flush=True)
                if output is not None:
                    output.write(line + "\n")
                    output.flush()
    finally:
        if output is not None:
            output.close()


if __name__ == "__main__":
    main()
