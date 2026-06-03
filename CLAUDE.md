# Autoform Plugin

## What it does

Autoform turns any AI coding assistant into a Lean 4 formalization agent. It provides Mathlib conventions, proof strategies, structured review checklists, statement extraction, and MCP tool servers for workspace inspection, Lean REPL, Mathlib search, LSP diagnostics, execution tracing, and Aristotle delegation.

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
│   ├── plugin.json                    #   Plugin + hooks + MCP server declarations
│   └── marketplace.json               #   Marketplace listing
│
├── .codex-plugin/                     # Codex CLI plugin manifest
│   └── plugin.json                    #   Rich metadata (interface, icons, defaultPrompt)
│
├── hooks/                             # Session hooks
│   └── session-start                  #   Bash — injects autoform context on session start
│
├── assets/                            # Icons and branding
│   ├── autoform.svg                   #   512x512 logo
│   └── autoform-small.svg             #   64x64 composer icon
│
├── skills/                            # All skills (single source of truth)
│   ├── autoform/SKILL.md              #   Core Mathlib & Lean 4 conventions
│   ├── autoform-prove/SKILL.md        #   Proof strategies & workflow
│   ├── autoform-review/SKILL.md       #   Formalization review checklist
│   ├── autoform-quality/SKILL.md      #   Code quality / style review
│   ├── autoform-extract/SKILL.md      #   Statement extraction from LaTeX
│   └── autoform-crew/SKILL.md         #   Parallel orchestration with subagent teams
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
│   ├── autoform-extract.toml
│   └── autoform-crew.toml
│
├── servers/                           # MCP tool servers (Python/FastMCP)
│   ├── workspace/                     #   Workspace inspection — project scan, targets, declarations
│   │   ├── core.py                    #     Pure logic (no MCP imports)
│   │   └── server.py                  #     FastMCP server
│   ├── repl/                          #   Lean REPL pool — run code, verify proofs
│   │   ├── core.py                    #     LeanRepl subprocess + response formatting
│   │   ├── pool.py                    #     LeanReplPool thread pool
│   │   └── server.py                  #     FastMCP server
│   ├── mathlib/                       #   Mathlib search — grep, find_name, read_file
│   │   ├── core.py                    #     Pure search logic (ripgrep-based)
│   │   └── server.py                  #     FastMCP server
│   ├── lsp/                           #   Lean LSP — file diagnostics, hover
│   │   └── server.py                  #     LeanLspSession + FastMCP server
│   ├── trace/                         #   Execution tracing — JSONL event store
│   │   ├── core.py                    #     TraceStore append-only JSONL
│   │   └── server.py                  #     FastMCP server
│   └── aristotle/                     #   Aristotle (Harmonic) — autonomous prover delegation
│       └── server.py                  #     AristotleManager + FastMCP server
│
└── viewer/                            # Standalone trace viewer (not part of plugin)
    └── (TODO)
```

## Architecture: server per concern

Each MCP server is independent and can run in a separate process:

| Server | Process cost | When needed | Agent |
|--------|-------------|-------------|-------|
| `autoform-workspace` | Stateless file scan, lightweight | First step — triage a project | any |
| `autoform-repl` | Spawns Lean processes, pools them, ~500MB+ RAM | Proving, compilation checking | worker |
| `autoform-mathlib` | Reads index on disk, stateless | Any agent searching Mathlib | worker, reviewer |
| `autoform-lsp` | Long-running language server, stateful sessions | Diagnostics, type info | reviewer |
| `autoform-trace` | Append-only JSONL, lightweight | Any agent recording progress | worker, reviewer |
| `autoform-aristotle` | HTTP calls to Harmonic API | Hard proofs delegated externally | worker (via crew) |

Agents declare their server subset in the `mcpServers` frontmatter field. The reader agent uses no MCP servers.

## Hooks

**`hooks/session-start`** — Bash script run on Claude Code session start. Injects a one-liner reminding the assistant that autoform skills and tools are available. Silent-fails on errors.

## Single source of truth

- **Skills:** edit `skills/<name>/SKILL.md`. Each has a paired `README.md` for humans.
- **Agents:** edit `agents/<name>.md`. Frontmatter declares tools, mcpServers, and model.
- **MCP servers:** edit `servers/<name>/`. Each server has `core.py` (logic) and `server.py` (FastMCP wrapper).
- **Commands:** edit `commands/<name>.toml`.
- **Hooks:** edit `hooks/<name>`. Bash scripts, must be executable.

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
| `LEAN_PROJECT_DIR` | `.` | workspace, repl, mathlib, lsp |
| `LEAN_REPL_CMD` | `lake exe repl` | repl |
| `LEAN_NUM_REPLS` | auto (from RAM) | repl |
| `AUTOFORM_TRACE_DIR` | `./traces` | trace |
| `AUTOFORM_RUN_ID` | `default` | trace |
| `ARISTOTLE_API_KEY` | — | aristotle |
| `ARISTOTLE_DOWNLOAD_DIR` | `./aristotle-output` | aristotle |
