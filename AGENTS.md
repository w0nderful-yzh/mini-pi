# AGENTS.md

Constraints for AI coding agents working on `mini-pi`. Current status, next tasks and
acceptance criteria live in the [Phase 3 plan](docs/plans/phase3-runtime-hardening.md);
delivered capabilities and usage limits live in the [README](README.md). Do not copy
milestone history or delivery logs into this file.

## Project boundaries

- Build a lightweight coding-agent harness in Python: the model decides the next step from tool observations. Never hard-code a `read → edit → test` flow.
- Stack: Python 3.12+, uv, Pydantic v2, pytest, sync OpenAI SDK, Typer, Rich, prompt_toolkit, ripgrep-bin. Providers: the OpenAI and DeepSeek OpenAI-compatible APIs only.
- No agent frameworks (LangGraph, AutoGen, CrewAI, Dify, Coze). Do not pull in a plugin framework, database, RAG, vector store, or parallel tool execution ahead of a real need.
- macOS/Linux only. Windows, other providers, MCP servers, remote transports and general multi-agent scheduling require an explicit request and a separate design.

## Architecture and facts

`CLI → AgentSession → Agent → run_loop → (LLM, ToolRegistry) → Tool → Workspace`

- CLI handles input, arguments and AgentEvent rendering. It never selects tools, calls tools directly, or writes UI art, spinners, usage text or session paths into model messages.
- Agent owns `AgentState` and calls the loop; it never reads or writes files or runs shell commands. The loop drives LLM/tool iterations and never reads stdin, renders output or depends on JSONL.
- Session assembles the runtime and persists/restores complete messages; Context owns project rules, the next-request prediction and the compaction projection. The LLM layer speaks provider protocols only; tools are dispatched through the registry and every file operation goes through Workspace.
- Complete system/user/assistant/tool messages are written to disk through `on_message_commit` before entering memory, and a failed write stops the run. JSONL records are never rewritten for display or compaction, and tool calls/results always stay paired.
- `RunContext` holds per-`run()` state only. `prepare_next_turn` runs after a complete tool batch and turn end and may return a replacement projection for the next request; the loop still computes the task budget. Do not add loop branches or new hook systems casually.

## Loop, errors, cancellation

- Termination reasons are `completed / step_limit / budget_limit / error / cancelled`. CLI output and one-shot exit codes must reflect them; never present unfinished or cancelled work as success.
- Expected `ToolError` becomes an `is_error` ToolMessage; expected LLM errors become `ErrorEvent`; anything else is a program defect that bubbles up. No swallowed exceptions, silent fallbacks or auto-corrected arguments.
- A `length` stop must not execute possibly truncated tool calls; record error observations to keep pairing. A user interrupt must not fabricate an assistant message: keep committed messages, tool results and file changes, and record cancelled observations for interrupted and unexecuted tool calls.
- `bash` timeouts and interrupts must kill the whole process group; non-zero exit codes and stderr are reported as-is. Default display is bounded and redacted; `--verbose` must not exceed what a tool captured.

## Tools, Workspace, shell

- One responsibility and one file per tool, with Pydantic argument models and registry lookup/validation. `ToolResult.content` goes to the model, `details` is UI-only, and `modified_files` lists only workspace-relative paths a tool actually changed — never guess.
- Workspace resolves a path and then verifies it is still inside the root; `../`, absolute paths and symlink escapes raise `WorkspaceViolationError`. Never rewrite a bad path into a safe one. File writes replace atomically.
- `edit` requires exact, unique matches: empty old text, no match, multiple matches, overlapping edits and no-op replacements all fail explicitly, applied back to front. `read` and `bash` keep their line/byte caps and truncation hints.
- File tools are bounded by Workspace; `bash` is an unsandboxed local shell with only cwd fixed, and it can reach outside the workspace and the network. Never call it a sandbox in README, CLI output or plans. Changing a security boundary needs its own design and verification.

## LLM, Session, Context

- Streaming tool calls are aggregated by index with explicit JSON parsing, and nothing is retried after the first emitted event. Retry 408/409/429, 5xx and network errors at most twice; never retry auth or ordinary argument errors. DeepSeek `reasoning_content` replay differences stay inside `DeepSeekClient`.
- API keys come only from environment variables or the user-level `~/.mini-pi/auth.json`, and never enter the project, logs or sessions. Auth files and input history keep restrictive permissions.
- JSONL sessions load strictly: a corrupt candidate is never silently skipped, and resume replays the active parent chain. Session fork is not implemented; requirements and prerequisites are in Phase 3.
- A task reads `AGENTS.md` only along the git-root-to-workspace ancestor chain, records rule changes as prompt section patches, and never scans the repository at startup. Read failures are reported before the user message is committed.
- The next request's window decision and task budget share one snapshot of the messages and tool schemas about to be sent (`RequestSnapshot`). Provider `usage.input_tokens` is historical measurement; the next-request prediction is an estimate, and last turn's `usage.total_tokens` is not this turn's input. Tool cost must accumulate per tool (one-time tool-mode overhead + per-tool framing + schema text); a fixed overhead alone under-estimates as tools grow. Unknown windows disable automatic compaction.
- Compaction replaces only a rebuildable model projection at a safe cut point: raw JSONL is never deleted, tool call/result pairs are never split, and `modified_files` is never lost. A window-triggered summary or write failure ends the run; a cost-triggered summary failure only drops the optimization, while a write failure still ends it. Never present offline cost estimates as real savings.

## Coding and verification

- Full type annotations, clear responsibilities, small functions, composition. No Manager/Factory/Adapter abstractions without a real need.
- Give every function, method and property (including private ones and `__init__`) a one-line Chinese docstring, and every module and public class a one-line Chinese summary. Non-obvious protocol, retry, truncation and safety-boundary logic needs an inline comment explaining why. No filler comments and no dead commented-out code.
- Default pytest runs offline: Agent/Loop tests use a fake LLM and file tests use `tmp_path`. Real API tests carry `@pytest.mark.integration`; `tests/integration/` is offline end-to-end and marker-free.
- After a code change, run the focused tests and then `uv run pytest`, `uv run ruff check .`, `uv run python -m compileall -q mini_pi` and `git diff --check`. Record real-provider or manual CLI results only when they actually ran.

## Documentation policy

Do not create or update documentation for routine code changes.

Only update persistent documentation when:

- architecture changes;
- public interfaces change;
- an important design decision is made;
- benchmark results establish a new baseline.

Do not create per-task implementation reports, completion reports, acceptance reports or
progress logs unless explicitly requested. Benchmark details stay under `docs/benchmarks/`
and are not loaded during normal development unless relevant to the current task. Prefer
updating an existing summary document over creating a new one. Documentation is not a
substitute for code, tests or git history, and must not restate what the code trivially
shows.

README section 8 is the milestone status table, and the Phase 3 plan holds the next tasks
and acceptance criteria; keep those two current when a milestone lands instead of writing a
delivery log. Read the
[Pi production architecture reference](docs/design/pi-production-architecture.md) and
README section 2 before changing Agent Core, Session or Context design, and keep them in
sync when a design decision changes.

## Commits

- Message format `<type>: <Chinese summary>`, with `type` limited to `feat`, `fix` or `docs`.
- Keep code and any documentation the policy above requires in the same commit.
- Never claim tests passed without running them.
- Historical deliveries and the current route: [Phase 1](docs/plans/phase1-core-runtime.md),
  [Phase 2](docs/plans/phase2-session-context.md),
  [Phase 3](docs/plans/phase3-runtime-hardening.md). An explicit user request overrides the
  general development order in this file.
