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

### Step 1: Extract targets

```
/autoform-extract

Extract formalizable statements from book.md into targets.yaml
```

This produces a structured YAML file with definitions, theorems, lemmas — each with an ID, source reference, LaTeX statement, dependencies, and whether the book provides a proof.

### Step 2: Formalize definitions first

Pick definitions in dependency order:

```
/autoform

Formalize Definition 1.1 (Convex set) from targets.yaml.
Search Mathlib first — this likely already exists.
```

The agent will:
1. Search Mathlib with `mathlib_grep` / `mathlib_find_name` to check if the concept exists
2. If not, write the Lean 4 definition following Mathlib conventions
3. Verify it compiles with `run_lean_code`
4. Write the `.lean` file

### Step 3: Prove theorems

```
/autoform-prove

Prove Theorem 1.2 (Convex combination characterization).
The definition it depends on is in MyBook/ConvexSets.lean.
```

The agent will:
1. Read the source statement from the book
2. Search Mathlib for relevant lemmas
3. Prototype proof fragments in the REPL (`run_lean_code`)
4. Iterate until the proof compiles without `sorry`

### Step 4: Review

```
/autoform-review

Review MyBook/ConvexSets.lean against Section 1 of book.md.
Check faithfulness, cheating patterns, and sorry/axiom usage.
```

The review checks:
- Compilation (via LSP diagnostics)
- Faithfulness to source material
- Mathematical correctness
- Mathlib conventions
- Cheating patterns (trivial substitution, smuggled assumptions, etc.)
- Proper use of `unproved` vs `sorry` vs `axiom`

### Step 5: Quality check (optional)

```
/autoform-quality

Check MyBook/ConvexSets.lean for Mathlib style compliance.
```

Pure style review — naming, tactics, proof structure. Does not evaluate mathematical correctness.

### Step 6: Repeat

Work through `targets.yaml` in dependency order — definitions before the theorems that use them.

## Batch formalization with subagents

For semi-automated work on multiple targets:

```
For each of these 5 definitions from Chapter 1 of targets.yaml,
spawn an autoform-worker subagent to formalize it.
Search Mathlib first, write to MyBook/Chapter1/.
```

## Slash commands reference

| Command | What it does |
|---------|-------------|
| `/autoform` | Load Mathlib & Lean 4 conventions |
| `/autoform-prove` | Load proof strategies and workflow guidance |
| `/autoform-review` | Review formalization for correctness and integrity |
| `/autoform-quality` | Check code quality against Mathlib conventions |
| `/autoform-extract` | Extract statements from source material |

## MCP tools available

| Tool | Server | What it does |
|------|--------|-------------|
| `run_lean_code` | repl | Run Lean code and return diagnostics |
| `get_repl_status` | repl | Check REPL pool health |
| `mathlib_grep` | mathlib | Search Mathlib source by pattern |
| `mathlib_find_name` | mathlib | Find declarations by name |
| `mathlib_read_file` | mathlib | Read a Mathlib source file |
| `lean_diagnostic_messages` | lsp | Get file compilation diagnostics |
| `lean_hover` | lsp | Get type info at a position |
| `record_proof_attempt` | trace | Record a proof attempt |
| `record_step` | trace | Record an agent action |
| `record_review` | trace | Record a review decision |
| `get_progress` | trace | Get run summary |
| `get_proof_attempts` | trace | Get recent proof attempts |
| `get_reviews` | trace | Get recent reviews |

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `LEAN_PROJECT_DIR` | `.` | Lean project directory for REPL, mathlib, LSP |
| `LEAN_REPL_CMD` | `lake exe repl` | Command to start the REPL |
| `LEAN_NUM_REPLS` | auto (from RAM) | Number of parallel REPL instances |
| `AUTOFORM_TRACE_DIR` | `./traces` | Directory for trace JSONL files |
| `AUTOFORM_RUN_ID` | `default` | Current run identifier |

## Comparison with the full autoform bot

| Full bot | Plugin |
|---|---|
| DAG runner dispatches tasks automatically | You pick the next target |
| 5+ worker agents in parallel on separate worktrees | One agent at a time (subagents for batching) |
| Automatic review → reject → retry loops | You run `/autoform-review` and decide |
| Multi-node SLURM scaling | Single machine |
| Persistent task tracker with status lifecycle | Trace server records progress |
| Visualizer dashboard | Trace JSONL (viewer TODO) |

The plugin is best for **focused formalization sessions** — a chapter at a time, interactively. For full-book autonomous formalization, rebuild the orchestration layer on top of the plugin's MCP servers.
