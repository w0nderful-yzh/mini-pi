"""M8.2 单段 Prompt 对照：固定 M8.0 初态并交错运行基线与候选。"""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

PROJECT = Path(__file__).resolve().parents[2]
BASELINE_COMMIT = "30aeb23"
BATCH_RULE = (
    "- When several necessary read-only checks have fully known arguments and do not "
    "depend on each other's results, issue their tool calls in the same response. "
    "Tools run sequentially; wait for observations before dependent calls or changes. "
    "Do not add unrelated checks just to form a batch."
)
sys.path.insert(0, str(PROJECT))

from mini_pi.auth import resolve_api_key  # noqa: E402


def _driver() -> ModuleType:
    """复用 M8.0 的固定初态、执行、外部判定与指标，不复制判定逻辑。"""
    path = Path(__file__).with_name("m8-0-eval.py")
    spec = importlib.util.spec_from_file_location("m8_2_base_driver", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def baseline_rules() -> tuple[str, ...]:
    """从固定提交解析规则常量，不执行历史 Python 代码。"""
    source = subprocess.run(
        ["git", "-C", str(PROJECT), "show", f"{BASELINE_COMMIT}:mini_pi/agent/prompt.py"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "_RULES" for target in node.targets
        ):
            rules = ast.literal_eval(node.value)
            if not isinstance(rules, tuple) or not all(isinstance(rule, str) for rule in rules):
                raise ValueError("baseline _RULES must be a tuple of strings")
            return rules
    raise ValueError("baseline _RULES was not found")


def variants() -> dict[str, tuple[str, ...]]:
    """候选只在固定基线末尾增加一条检查合批规则。"""
    baseline = baseline_rules()
    return {"baseline": baseline, "batched": (*baseline, BATCH_RULE)}


def main() -> None:
    """每条任务交错执行两种规则，逐次保存实测和相同初态的身份。"""
    driver = _driver()
    parser = argparse.ArgumentParser(description="M8.2 paired prompt evaluation")
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--only", action="append", choices=[task.name for task in driver.TASKS])
    parser.add_argument("--jsonl", type=Path, required=True)
    args = parser.parse_args()
    if args.repeats <= 0:
        parser.error("--repeats must be positive")
    key = resolve_api_key(driver.PROVIDER, env_var="DEEPSEEK_API_KEY")
    if not key:
        raise SystemExit("DeepSeek API Key 不可用：真实对照未执行")
    rules = variants()
    runtime_commit = subprocess.run(
        ["git", "-C", str(PROJECT), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    selected = [task for task in driver.TASKS if not args.only or task.name in args.only]
    with args.jsonl.open("w", encoding="utf-8") as output:
        for task in selected:
            for repeat in range(1, args.repeats + 1):
                order = ("baseline", "batched") if repeat % 2 else ("batched", "baseline")
                fixture_id: str | None = None
                for variant in order:
                    print(f"running {task.name} {repeat} {variant}", file=sys.stderr, flush=True)
                    record = driver.run_task(
                        task, repeat=repeat, key=key,
                        max_steps=20 if task.fixture is None else 12,
                        prompt_rules=rules[variant],
                    )
                    if fixture_id is not None and record["fixture_id"] != fixture_id:
                        raise RuntimeError("paired variants used different fixtures")
                    fixture_id = record["fixture_id"]
                    record.update(
                        variant=variant,
                        runtime_commit=runtime_commit,
                        baseline_prompt_commit=BASELINE_COMMIT,
                        rules_sha256=hashlib.sha256("\n".join(rules[variant]).encode()).hexdigest(),
                    )
                    line = json.dumps(record, ensure_ascii=False, sort_keys=True)
                    output.write(line + "\n")
                    output.flush()
                    print(line, flush=True)


if __name__ == "__main__":
    main()
