---
name: plugin-insights
description: "Analysis of four Claude Code plugin architectures (caveman, superpowers, lean4-skills, mathlib-quality) stored in inspiration/plugin/"
metadata: 
  node_type: memory
  type: reference
  originSessionId: e1f8ec53-c526-4c46-b43f-f99da0e967f1
---

# Plugin Architecture Insights

Four Claude Code plugins cloned under `inspiration/plugin/` (gitignored) as reference. None are installed.

| Plugin | Repo | Author | Version |
|---|---|---|---|
| caveman | `JuliusBrussee/caveman` | Julius Brussee | — |
| superpowers | `obra/superpowers` | Jesse Vincent | 5.1.0 |
| lean4-skills | `cameronfreer/lean4-skills` | Cameron Freer | 4.4.10 |
| mathlib-quality | `CBirkbeck/mathlib-quality` | CBirkbeck | 0.43.1 |

---

## 1. Caveman — Output Token Compression

**Purpose:** Makes AI respond in compressed caveman-speak — cuts ~75% output tokens, full technical accuracy.

**Key mechanisms:**
- **SessionStart hook** (`caveman-activate.js`) writes flag file + injects caveman rules as system context
- **UserPromptSubmit hook** (`caveman-mode-tracker.js`) tracks `/caveman` slash commands and natural-language triggers ("talk like caveman" / "normal mode")
- **Statusline script** shows `[CAVEMAN] ⛏ 12.4k` with lifetime savings
- **Flag file** at `$CLAUDE_CONFIG_DIR/.caveman-active` persists mode across turns

**Intensity levels:** `lite` (drop filler), `full` (default), `ultra` (telegraphic), `wenyan` (classical Chinese)

**Repo structure:**
```
caveman/
├── .claude-plugin/plugin.json     # Plugin manifest (hooks wiring)
├── skills/                        # SKILL.md per skill (single source of truth)
│   ├── caveman/                   #   core compression behavior
│   ├── caveman-commit/            #   commit message formatting
│   ├── caveman-review/            #   code review one-liners
│   ├── caveman-compress/          #   rewrite CLAUDE.md files into caveman-speak
│   ├── caveman-stats/             #   token usage tracking
│   ├── caveman-help/              #   quick reference card
│   └── cavecrew/                  #   subagent delegation guide
├── agents/                        # Subagent definitions (investigator/builder/reviewer)
├── src/
│   ├── hooks/                     # JS hooks + statusline scripts
│   ├── rules/                     # Always-on activation rule body
│   ├── tools/                     # caveman-init.js (per-repo rule writer)
│   ├── mcp-servers/               # caveman-shrink (MCP middleware)
│   └── plugins/opencode/          # opencode native plugin
├── bin/install.js                 # Unified installer for 30+ agents
├── plugins/caveman/               # CI-mirrored distribution copy
├── benchmarks/                    # Real API token measurements
├── evals/                         # Three-arm eval harness (baseline/terse/skill)
└── commands/                      # TOML stubs for Codex/Gemini
```

**Multi-agent reach:** Claude Code (plugin+hooks), Codex (plugin), Gemini (extension), OpenClaw (SOUL.md bootstrap), opencode (native plugin), Cursor/Windsurf/Cline/Copilot (via `npx skills`), and 30+ others via unified installer.

**Design patterns worth noting:**
- Flag-file-based state persistence across turns (simple, robust)
- SessionStart stdout injection for invisible system context
- Per-turn reinforcement via UserPromptSubmit `hookSpecificOutput` (attention anchor)
- Symlink-safe flag writes (`safeWriteFlag`) for security
- Three-arm eval harness (baseline/terse/skill) — honest delta = skill vs terse, not skill vs verbose
- CI sync workflow mirrors skills/ → plugins/ distribution copy
- `bin/lib/settings.js` — JSONC-tolerant settings.json reader/writer (handles comments)

---

## 2. Superpowers — Software Development Methodology

**Purpose:** Complete dev methodology — brainstorming through TDD to PR merge. Skills auto-trigger based on context.

**The workflow:** brainstorming → git worktree → writing plans → subagent-driven development → TDD → code review → finishing branch. Each step is a skill that activates automatically.

**Repo structure:**
```
superpowers/
├── .claude-plugin/plugin.json     # Plugin manifest (no hooks — pure skills)
├── .codex-plugin/plugin.json      # Codex distribution
├── .cursor-plugin/plugin.json     # Cursor distribution
├── .opencode/                     # opencode plugin adapter
├── skills/                        # 12 skills, each a dir with SKILL.md
│   ├── brainstorming/             #   Socratic design refinement (+ visual companion)
│   ├── writing-plans/             #   Detailed implementation planning
│   ├── executing-plans/           #   Batch execution with checkpoints
│   ├── subagent-driven-development/ # Fresh subagent per task, two-stage review
│   ├── dispatching-parallel-agents/ # Concurrent subagent workflows
│   ├── test-driven-development/   #   RED-GREEN-REFACTOR cycle
│   ├── systematic-debugging/      #   4-phase root cause process
│   ├── verification-before-completion/ # Ensure it's actually fixed
│   ├── requesting-code-review/    #   Pre-review checklist
│   ├── receiving-code-review/     #   Responding to feedback
│   ├── using-git-worktrees/       #   Parallel development branches
│   ├── finishing-a-development-branch/ # Merge/PR decision
│   ├── writing-skills/            #   Meta: how to write new skills
│   └── using-superpowers/         #   Bootstrap / intro
├── hooks/                         # SessionStart hook (loads bootstrap)
├── AGENTS.md / GEMINI.md          # Cross-agent discovery files
├── scripts/                       # Build/sync scripts
└── tests/                         # Multi-harness test suites
```

**Design patterns worth noting:**
- **Pure skills, no MCP servers** — the simplest architecture of the four
- `using-superpowers` skill acts as bootstrap — triggers other skills based on context
- Multi-harness distribution via per-harness plugin dirs (`.claude-plugin/`, `.codex-plugin/`, `.cursor-plugin/`)
- Very strict contribution policy (94% PR rejection rate, documented in CLAUDE.md with detailed anti-slop rules)
- Skills include reference materials alongside SKILL.md (e.g. `testing-anti-patterns.md`, `defense-in-depth.md`)
- Subagent-driven-development uses two-stage review: spec compliance then code quality

---

## 3. Lean 4 Skills — Theorem Proving Workflows

**Purpose:** Structured prove/review/golf loop for Lean 4. Host-agnostic — same core skill across Claude Code, Codex, Gemini, Cursor.

**Workflows:** `draft` → `formalize`/`autoformalize` → `prove`/`autoprove` → `review` → `refactor` → `golf` → `checkpoint`

**Repo structure:**
```
lean4-skills/
├── .claude-plugin/marketplace.json   # Marketplace registration
├── plugins/
│   ├── lean4/                        # Main plugin
│   │   ├── .claude-plugin/plugin.json
│   │   ├── skills/lean4/SKILL.md     # Core skill reference (all 11 workflows)
│   │   ├── commands/                 # Per-workflow command docs
│   │   ├── hooks/                    # UserPromptSubmit hook (input validation)
│   │   ├── lib/command_args/         # Host-agnostic CLI parser
│   │   ├── scripts/                  # Helper scripts
│   │   ├── tools/                    # MCP tools
│   │   ├── agents/                   # Subagent definitions
│   │   └── tests/
│   └── lean4-contribute/             # Separate plugin for filing issues/insights
│       ├── .claude-plugin/plugin.json
│       ├── commands/                 # /bug-report, /feature-request, /share-insight
│       └── tools/
├── README.md
├── INSTALLATION.md                   # Per-host setup guides
└── TESTING.md
```

**Design patterns worth noting:**
- **Two sub-plugins** in one repo (`lean4` + `lean4-contribute`), each with own `.claude-plugin/`
- Shared cycle engine: Plan → Work → Checkpoint → Review → Replan → Continue/Stop — used by both `prove` (interactive) and `autoprove` (autonomous)
- Host-agnostic CLI parser (`lib/command_args/`) validates inputs before model sees them
- UserPromptSubmit hook does pre-validation of `/lean4:*` commands (rejects invalid startup configs)
- External MCP dependency (lean-lsp-mcp) recommended but not bundled — plugin works standalone
- `lean4-contribute` plugin lets users file bug reports / feature requests / share insights from within the editor

---

## 4. Mathlib Quality — Mathlib Standards Enforcement

**Purpose:** Full development + cleanup + PR workflow for mathlib contribution standards. Built on 14,000+ real PR review comments with 7,273 before/after code suggestions.

**Two-phase development:**
- **`/develop`** — planning only. Mathlib search, API design, decomposition with source quotes, feasibility checks. Creates ticket board. No execution.
- **`/beastmode`** — marathon execution. Picks tickets, spawns sub-tickets, replans, no recursion cap, no time budget. Stop hook prevents turn-ending mid-marathon.

**10-phase `/cleanup`:** Doctor → Prepare → Style audit → File-level fixes → Per-declaration deep golf (18-item audit, one agent per decl) → Refactoring (5a non-rename + 5b rename pass) → Final gates → Simplify hand-off → Report

**Repo structure:**
```
mathlib-quality/
├── .claude-plugin/plugin.json     # Plugin manifest
├── .mcp.json                      # MCP server config (RAG)
├── commands/                      # 18 slash commands (develop, beastmode, cleanup, etc.)
├── skills/mathlib-quality/
│   ├── SKILL.md                   # Main skill activation triggers
│   ├── references/                # Authoritative docs (style, naming, golfing, patterns, etc.)
│   ├── agents/                    # Subagent definitions
│   ├── examples/                  # Worked examples
│   └── learning/                  # Teaching/learning resources
├── mcp_server/
│   └── mathlib_rag.py             # RAG MCP server over 5,752 indexed PR-feedback examples
├── hooks/
│   ├── hooks.json                 # Hook configuration
│   └── beastmode_stop.sh          # Prevents turn-ending during marathon sessions
├── scripts/                       # Scraping, analysis, index-building tools
├── data/
│   ├── pr_feedback/               # RAG indexes from 3,772 merged PRs
│   └── community_learnings/       # User-contributed patterns
└── setup.sh
```

**Design patterns worth noting:**
- **Data-driven rules:** 7,273 before/after code suggestions from real mathlib PRs → concrete golfing rules
- **Anti-skip enforcement via required artifacts:** status blocks agents must emit, verification gates between phases, diff gates on edits. Enforcement through structure, not guidelines.
- **RAG MCP server** (`mathlib_rag.py`) for searching PR feedback examples
- **Optional ChatGPT MCP** for mathematical second opinions
- **Stop hook** (`beastmode_stop.sh`) — while marathon is active, refuses turn-end and re-prompts the agent. Gated by sentinel file (`.mathlib-quality/beastmode_active`), fail-safe for non-beastmode sessions.
- **Orchestrator-worker pattern** for `/cleanup-all` — main session dispatches, never reads files or runs tools directly. Sustained a real 28-day, 9000-message, 395-dispatch marathon.
- **Rename queue** — Phase-4 workers never rename in place (parallel race condition). Append to `.mathlib-quality/renames.jsonl`, Phase 5b drains sequentially.
- **Diff-level gating** borrowed from shouyi — catches policy violations (touched a def that shouldn't change, modified a theorem statement during proof-only golf), not just type errors.
- **Builds on lean4-skills** — recommends installing both together.

---

## Cross-Cutting Comparison

| | Caveman | Superpowers | Lean 4 Skills | Mathlib Quality |
|---|---|---|---|---|
| **Domain** | Any (token compression) | General software dev | Lean 4 proving | Lean 4 mathlib contribution |
| **Mechanism** | Hooks + skills | Pure skills | Skills + hooks + tools | Commands + hooks + MCP + data |
| **Hooks** | SessionStart + UserPromptSubmit | SessionStart (bootstrap) | UserPromptSubmit (validation) | Stop (beastmode keep-alive) |
| **MCP servers** | caveman-shrink (middleware) | None | None (external lean-lsp-mcp) | mathlib_rag.py (RAG) |
| **Multi-agent** | 30+ via unified installer | Claude Code, Codex, Gemini, Cursor, OpenCode | Claude Code, Codex, Gemini, Cursor, Windsurf | Claude Code only |
| **Complexity** | Light | Medium | Medium-heavy | Very heavy |
| **Data-driven** | Benchmarks + evals | No | No | 14K+ PR review comments |
| **Subagents** | cavecrew (investigator/builder/reviewer) | SDD (per-task + two-stage review) | Per-workflow | Per-declaration cleanup workers |

## Plugin System Anatomy (from these examples)

A Claude Code plugin lives in a directory with `.claude-plugin/plugin.json` as its manifest. The manifest declares:

1. **`name`** + **`description`** — identity
2. **`hooks`** — `SessionStart`, `UserPromptSubmit`, `Stop` event handlers (commands that run on events)
3. Skills are auto-discovered from `skills/<name>/SKILL.md` files
4. Commands from `commands/<name>.md` files
5. Agents from `agents/<name>.md` files
6. MCP servers via `.mcp.json` at plugin root

**Distribution patterns observed:**
- Claude Code: `.claude-plugin/` dir
- Codex: `.codex-plugin/` dir or `plugins/<name>/.codex-plugin/`
- Gemini: `gemini-extension.json` + `GEMINI.md`
- Cursor: `.cursor-plugin/` dir
- opencode: `.opencode/` dir with JS plugin
- Generic agents: `AGENTS.md` at repo root
- Cross-agent: `npx skills add` (vercel-labs/skills ecosystem)
