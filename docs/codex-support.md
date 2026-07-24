# Codex support: current state and what a full port would take

**Status: Claude Code is the supported harness. Codex can run part of the stack today; full parity is a deliberate refactor, not a file-format change.** This note explains why, so the decision to build it (or not) is made with eyes open.

## The plugin is two layers

Everything autoform does splits cleanly into a harness-agnostic core and a Claude-Code-native orchestration layer. Only the second is Claude-specific.

### Layer 1 — deterministic core (already harness-agnostic)
Plain Python, no model in the loop; runs identically no matter what launches it:

- `scripts/dispatch_runner.py` — the parallel review-jury + prover-worker engine
- `scripts/serve_review.py` — the review dashboard (a stdlib HTTP server)
- `scripts/dispatch_queue.py`, `scripts/merge_node.py` — the task queue + the single graph writer
- `servers/prover/verify.py` — the kernel honesty gate (`lake build` + `#print axioms`)
- `servers/prover/driver.py` over `ProverAdapter` (`base.py`) — swappable backends: `claude`, `aristotle`, `codex`, `openai`/`avocado`
- the `graph.json` DAG format

Because of this, **the prove → verify → jury → dashboard loop already works from Codex** via `dispatch_runner.py --backend codex` (and `openai`/`avocado`). Nothing here needs porting.

### Layer 2 — AI-orchestration (Claude-Code-native)
This is what `/autoform:setup` and `/autoform:orchestrate` *are*, and it is built entirely from Claude Code primitives:

- **`.md` commands** whose body is a procedural algorithm the model executes (with `allowed-tools`, arguments).
- **The Skill system** — `setup` invokes the `plan` / `make-project` / `plan-view` Skills (model-invoked `SKILL.md` procedures).
- **The `Task` subagent tool** — the plan pipeline and `orchestrate` spawn ~8 subagent types (`splitter`, `mathlib-checker`, `graph-reviewer`, `content-reviewer`, `holistic-reviewer`, `source-searcher`, plus the jury reviewers), defined as `agents/*.md`, launched in parallel, results merged via `merge_node.py`.
- **`${CLAUDE_PLUGIN_ROOT}`** and the `.mcp.json` MCP servers.

None of these has a drop-in Codex equivalent.

## What Codex offers today (and where it differs)

The plugin already ships a Codex surface: `.codex-plugin/plugin.json` and a Codex-format command (`commands/zulip.toml`), and `make install-codex` wires a local Codex marketplace.

- **Commands:** Codex commands are `.toml` **prompt templates** (`description` + `prompt`), not the procedural, tool-carrying `.md` commands Claude uses.
- **Skills:** no equivalent. The `plan`/`make-project`/`plan-view` skill logic has no Codex host.
- **Subagents:** Codex has no confirmed drop-in for Claude's `Task` tool (spawn a sub-agent with its own system prompt + tools, return structured output). **[open question — verify]** the closest is driving multiple `codex exec` processes from a script, which is exactly the pattern `dispatch_runner` already uses for the jury/prover.
- **Plugin root / MCP:** Codex uses its own plugin-root convention and MCP wiring **[verify]**, not `${CLAUDE_PLUGIN_ROOT}` / the Claude `.mcp.json`.

So adding `setup.toml`/`orchestrate.toml` gets you the *names* in Codex, but a `.toml` prompt cannot reproduce the Skill- and Task-driven procedure.

## Two ways to close the gap

### Cheap tier — Codex drives the engine
Add `commands/setup.toml` / `orchestrate.toml` as Codex prompts that launch the deterministic engine (`dispatch_runner.py --backend codex --watch --workers`) and enqueue work. You get prove → verify → jury → dashboard from Codex, **without** the Claude-native planning/autonomy niceties. Small, mostly-there already.

### Elegant tier — a `SubagentAdapter`, mirroring `ProverAdapter`
The proving layer already made backends pluggable behind one `driver.py`. Apply the same pattern one level up, to orchestration:

- a `SubagentAdapter` ABC — `spawn(spec, payload) -> Result` + a parallel `spawn_many(...)`, with `AgentSpec` (harness-neutral: name, system prompt, tools, model, output schema) and a structured `Result`;
- two adapters — `ClaudeTaskAdapter` (wraps today's `Task autoform:<agent>` behavior) and `CodexExecAdapter` (parallel `codex exec` with the spec's prompt inlined, JSON out, reusing the codex/openai subprocess machinery);
- the plan pipeline + orchestrate autonomy loop moved into **one harness-neutral Python driver** that calls `SubagentAdapter` for fan-out and the existing `dispatch_queue`/`merge_node`/`dispatch_runner` for the deterministic parts;
- the ~8 agents expressed as **harness-neutral specs** (data) instead of `agents/*.md`, rendered per harness;
- thin per-harness entrypoints: a Claude `.md` command and a Codex `.toml`.

This is the version that makes Codex a true peer for the full pipeline. It should be phased and regression-safe: **Phase 1** introduces the abstraction plus `ClaudeTaskAdapter` with **no observable change to Claude behavior** (the 387-test suite stays green); later phases move the logic into the shared driver, add `CodexExecAdapter` + Codex agent specs + `.toml` commands, and verify end-to-end on Codex.

## Open questions to resolve before building the elegant tier
- Does Codex expose a native parallel-subagent primitive with structured output, or must the fan-out be driven as parallel `codex exec` processes from Python?
- Does Codex support MCP servers, and how is the plugin root provided?
- The jury judges currently hardcode `claude -p` (`dispatch_runner.run_judge`); a Codex-only run needs a Codex judge path (or keep the judges on Claude/Max).
- Data-handling approval for sending textbook + Lean source to the `codex`/`avocado` endpoints (see `docs/avocado-handoff.md`).

## Recommendation
If Codex users only need the pipeline, ship the **cheap tier** first. The **elegant tier** is the right long-term design (it mirrors `ProverAdapter` and makes the orchestration harness-pluggable), but it is a multi-phase investment and depends on resolving the open questions above. This note is the record of that decision; no orchestration code has been changed to add Codex support.
