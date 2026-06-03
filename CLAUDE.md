# Autoform Plugin

## What it does

Autoform turns any AI coding assistant into a Lean 4 formalization agent. It provides Mathlib conventions, proof strategies, structured review checklists, statement extraction, and MCP tool servers for Lean REPL, Mathlib search, LSP diagnostics, and execution tracing.

## Layout

```
autoform-bot/
├── README.md                          # Product pitch
├── CLAUDE.md                          # This file (maintainer instructions)
├── LICENSE                            # MIT
├── pyproject.toml                     # Python package (servers only)
├── package.json                       # npm package metadata
├── AGENTS.md                          # Multi-agent autodiscovery
├── GEMINI.md                          # Gemini CLI autodiscovery
├── gemini-extension.json              # Gemini CLI extension manifest
│
├── .claude-plugin/                    # Claude Code plugin manifest
│   ├── plugin.json                    #   Plugin + MCP server declarations
│   └── marketplace.json               #   Marketplace listing
│
├── skills/                            # All skills (single source of truth)
│   ├── autoform/SKILL.md              #   Core Mathlib & Lean 4 conventions
│   ├── autoform-prove/SKILL.md        #   Proof strategies & workflow
│   ├── autoform-review/SKILL.md       #   Formalization review checklist
│   ├── autoform-quality/SKILL.md      #   Code quality / style review
│   └── autoform-extract/SKILL.md      #   Statement extraction from LaTeX
│
├── agents/                            # Subagent definitions
│   ├── autoform-worker.md             #   Formalization (opus) → repl, mathlib, trace
│   ├── autoform-reviewer.md           #   Review (opus) → lsp, mathlib, trace
│   └── autoform-reader.md             #   File reader (haiku) → no servers
│
├── commands/                          # Slash command stubs (Codex/Gemini)
│   ├── autoform.toml
│   ├── autoform-prove.toml
│   ├── autoform-review.toml
│   ├── autoform-quality.toml
│   └── autoform-extract.toml
│
├── servers/                           # MCP tool servers (Python/FastMCP)
│   ├── repl/                          #   Lean REPL pool — run code, verify proofs
│   │   ├── core.py                    #     LeanRepl subprocess + response formatting
│   │   ├── pool.py                    #     LeanReplPool thread pool
│   │   └── server.py                  #     FastMCP server (run_lean_code, get_repl_status)
│   ├── mathlib/                       #   Mathlib search — grep, find_name, read_file
│   │   ├── core.py                    #     Pure search logic (ripgrep-based)
│   │   └── server.py                  #     FastMCP server (mathlib_grep, mathlib_find_name, mathlib_read_file)
│   ├── lsp/                           #   Lean LSP — file diagnostics, hover
│   │   └── server.py                  #     LeanLspSession + FastMCP server
│   └── trace/                         #   Execution tracing — JSONL event store
│       ├── core.py                    #     TraceStore append-only JSONL
│       └── server.py                  #     FastMCP server (record_*, get_progress, get_*_attempts)
│
└── viewer/                            # Standalone trace viewer (not part of plugin)
    └── (TODO)
```

## Architecture: server per concern

Each MCP server is independent and can run in a separate process:

| Server | Process cost | When needed | Agent |
|--------|-------------|-------------|-------|
| `autoform-repl` | Spawns Lean processes, pools them, ~500MB+ RAM | Proving, compilation checking | worker |
| `autoform-mathlib` | Reads index on disk, stateless | Any agent searching Mathlib | worker, reviewer |
| `autoform-lsp` | Long-running language server, stateful sessions | Diagnostics, type info | reviewer |
| `autoform-trace` | Append-only JSONL, lightweight | Any agent recording progress | worker, reviewer |

Agents declare their server subset in the `mcpServers` frontmatter field. The reader agent uses no MCP servers — it only reads files.

## Single source of truth

- **Skills:** edit `skills/<name>/SKILL.md`. Each has a paired `README.md` for humans.
- **Agents:** edit `agents/<name>.md`. Frontmatter declares tools, mcpServers, and model.
- **MCP servers:** edit `servers/<name>/`. Each server has `core.py` (logic) and `server.py` (FastMCP wrapper).
- **Commands:** edit `commands/<name>.toml`.

## Adding a new skill

1. Create `skills/<new-skill>/SKILL.md` with YAML frontmatter (`name`, `description`)
2. Create `skills/<new-skill>/README.md` for humans
3. Add a `commands/<new-skill>.toml` for Codex/Gemini
4. Add `@skills/<new-skill>/SKILL.md` to `AGENTS.md` and `GEMINI.md`

## Adding a new MCP server

1. Create `servers/<name>/` with `__init__.py`, `core.py`, and `server.py`
2. `core.py` = pure logic (no MCP imports), `server.py` = FastMCP wrapper with `__main__` block
3. Add to `.claude-plugin/plugin.json` under `mcpServers`
4. Add to relevant agents' `mcpServers` frontmatter

## Environment variables

| Variable | Default | Used by |
|----------|---------|---------|
| `LEAN_PROJECT_DIR` | `.` | repl, mathlib, lsp |
| `LEAN_REPL_CMD` | `lake exe repl` | repl |
| `LEAN_NUM_REPLS` | auto (from RAM) | repl |
| `AUTOFORM_TRACE_DIR` | `./traces` | trace |
| `AUTOFORM_RUN_ID` | `default` | trace |
