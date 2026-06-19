# Usage Guide

How to use the autoform plugin to formalize mathematics in Lean 4 with Claude Code.

## Setup

### 1. Create a Lean project with Mathlib

```bash
mkdir my-formalization && cd my-formalization
lake init MyBook math
lake build
```

### 2. Start Claude Code with the plugin

```bash
LEAN_PROJECT_DIR=$(pwd) claude --plugin-dir /path/to/autoform-bot
```

Or make it permanent:

```bash
# Shell alias
alias claude-autoform='claude --plugin-dir /path/to/autoform-bot'

# Or symlink into skills directory
ln -s /path/to/autoform-bot ~/.claude/skills/autoform
```

### 3. Place your source material

Put your book as `book.md` (or `book.pdf`) in the project directory.

## Workflow

> **Note:** The workflow below describes the full vision. Steps marked ⬜ use
> skills or tools that are not yet implemented — see the tables at the bottom
> for current status. Steps marked ✅ work today.

### Step 0: Inspect the project ✅

```
/workspace
```

Scans your Lean project for sorry/axiom counts, declarations, and targets files.

### Step 1: Search Zulip for prior art ✅

```
/zulip

Search Zulip for "concentration inequality" to find naming conventions and existing work.
```

### Step 2: Extract targets ⬜

```
/autoform-extract

Extract formalizable statements from book.md into targets.yaml
```

This produces a structured YAML file with definitions, theorems, lemmas — each with an ID, source reference, LaTeX statement, dependencies, and whether the book provides a proof.

### Step 3: Formalize definitions first ⬜

Pick definitions in dependency order:

```
/autoform

Formalize Definition 1.1 (Convex set) from targets.yaml.
Search Mathlib first — this likely already exists.
```

### Step 4: Prove theorems ⬜

```
/autoform-prove

Prove Theorem 1.2 (Convex combination characterization).
The definition it depends on is in MyBook/ConvexSets.lean.
```

### Step 5: Review ⬜

```
/autoform-review

Review MyBook/ConvexSets.lean against Section 1 of book.md.
Check faithfulness, cheating patterns, and sorry/axiom usage.
```

### Step 6: Repeat

Work through `targets.yaml` in dependency order — definitions before the theorems that use them.

## Batch formalization with subagents

For semi-automated work on multiple targets:

```
For each of these 5 definitions from Chapter 1 of targets.yaml,
spawn an autoform-worker subagent to formalize it.
Search Mathlib first, write to MyBook/Chapter1/.
```

## Delegating to Aristotle

For hard, self-contained proofs, delegate to [Aristotle](https://aristotle.harmonic.fun)
(Harmonic's autonomous prover). Aristotle is a prover **backend**: it produces a
proof *into a plan node*, and that proof then feeds the same review pipeline
(jury → `review_status.json` → review surface) as the in-session worker. It is one
of two swappable backends behind a single interface — `(target node + spec) →
proof written back to the node` — alongside the in-session Claude worker.

> **Aristotle is a FREE external API, and it is OPT-IN / default-off.** There is
> no metered cost to you. The only requirements are the opt-in `aristotle` extra
> (`aristotlelib`), an `ARISTOTLE_API_KEY`, and network access. With no Python
> backend and a free Aristotle, the architecture is all-free (in-session work
> bills the Claude subscription, not the API). Nothing here runs unless you ask
> for it.

```bash
# Opt in: install the extra and set a (free) API key.
uv sync --extra aristotle
export ARISTOTLE_API_KEY=arstl_...   # free key: https://aristotle.harmonic.fun/dashboard/keys
```

### Delegate a plan node (recommended)

The highest-level entry hands Aristotle a target node from your plan and writes
the proof back into that node — landing the Lean files into the project and
recording the proof in the node's prose file. The returned `merge_payload` links
the node's `content` through the single locked graph writer (`merge_node.py`).

```
aristotle_delegate_node(
    graph_path="graph.json",
    node_id="Chernoff bound",
    project_dir=".")
```

Aristotle reads the node's spec itself (its informal statement, `source_refs`,
`mathlib_declarations`, and in-tier `depends_on`), so you don't restate it. The
landed proof is reviewed by the normal jury/review surface — Aristotle never
self-certifies, reviews, or touches `review_status.json`.

### Lower-level session tools

For ad-hoc tasks (not tied to a plan node), drive a raw session:

```
Submit Theorem 5.5 to Aristotle:

aristotle_submit("thm-5-5",
    "Prove: for all convex sets C in ℝⁿ, if C is bounded then C is compact.
     Write to MyBook/Compactness.lean. Use Mathlib's IsCompact and IsBounded.",
    project_dir=".")
```

While Aristotle works (can take minutes to hours), do other work. Check progress:

```
aristotle_events("thm-5-5")   # see what Aristotle is doing
aristotle_poll("thm-5-5")     # check status
```

Steer if needed:

```
aristotle_steer("thm-5-5", "Use Metric.isCompact_iff_isClosed_bounded instead of manual epsilon-net")
```

Collect the result:

```
aristotle_wait("thm-5-5")     # block until done
```

## Slash commands reference

| Command | What it does | Status |
|---------|-------------|--------|
| `/install-lean` | Install Lean 4, elan, lake | ✅ |
| `/setup-project` | Create new Lean 4 + Mathlib project | ✅ |
| `/workspace` | Inspect project structure and health | ✅ |
| `/zulip` | Search Lean Zulip for community discussions | ✅ |
| `/autoform` | Load Mathlib & Lean 4 conventions | ⬜ Not yet |
| `/autoform-prove` | Load proof strategies and workflow guidance | ⬜ Not yet |
| `/autoform-review` | Review formalization for correctness and integrity | ⬜ Not yet |
| `/autoform-quality` | Check code quality against Mathlib conventions | ⬜ Not yet |
| `/autoform-extract` | Extract statements from source material | ⬜ Not yet |
| `/autoform-crew` | Orchestrate parallel formalization with subagent teams | ⬜ Not yet |

## MCP tools available

| Tool | Server | What it does | Status |
|------|--------|-------------|--------|
| `zulip_search` | zulip | Search Zulip messages by keyword | ✅ |
| `zulip_messages` | zulip | Fetch messages from a stream/topic | ✅ |
| `zulip_streams` | zulip | List available streams | ✅ |
| `zulip_topics` | zulip | List topics in a stream | ✅ |
| `zulip_status` | zulip | Check .zuliprc configuration | ✅ |
| `run_lean_code` | repl | Run Lean code and return diagnostics | ⬜ Stub |
| `get_repl_status` | repl | Check REPL pool health | ⬜ Stub |
| `lean_diagnostic_messages` | lsp | Get file compilation diagnostics | ⬜ Stub |
| `lean_hover` | lsp | Get type info at a position | ⬜ Stub |
| `aristotle_delegate_node` | aristotle | Delegate a plan node: spec in, proof written back to the node | ✅ |
| `aristotle_submit` | aristotle | Submit a formalization task to Aristotle | ✅ |
| `aristotle_wait` | aristotle | Block until an Aristotle task completes | ✅ |
| `aristotle_poll` | aristotle | Non-blocking status check | ✅ |
| `aristotle_steer` | aristotle | Redirect a running task with new instructions | ✅ |
| `aristotle_events` | aristotle | Inspect what Aristotle is doing | ✅ |
| `aristotle_sessions` | aristotle | List all active Aristotle sessions | ✅ |

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `LEAN_PROJECT_DIR` | `.` | Lean project directory for REPL, LSP, Zulip config discovery |
| `LEAN_REPL_CMD` | `lake exe repl` | Command to start the REPL |
| `LEAN_NUM_REPLS` | auto (from RAM) | Number of parallel REPL instances |
| `ZULIPRC` | auto-discovered | Path to .zuliprc file (overrides discovery chain) |
| `ARISTOTLE_API_KEY` | — | Harmonic API key (required for Aristotle) |
| `ARISTOTLE_DOWNLOAD_DIR` | `./aristotle-output` | Where Aristotle downloads result files |

## Comparison with the full autoform bot

| Full bot | Plugin |
|---|---|
| DAG runner dispatches tasks automatically | You pick the next target |
| 5+ worker agents in parallel on separate worktrees | One agent at a time (subagents for batching) |
| Automatic review → reject → retry loops | You run `/autoform-review` and decide |
| Multi-node SLURM scaling | Single machine |

The plugin is best for **focused formalization sessions** — a chapter at a time, interactively. For full-book autonomous formalization, rebuild the orchestration layer on top of the plugin's MCP servers.
