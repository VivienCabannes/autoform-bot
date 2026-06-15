# Examples

This directory contains the full reference implementations for every stubbed component in the autoform plugin. Each subdirectory mirrors the top-level layout (`servers/`, `skills/`, `agents/`) and provides a complete, working version that you can copy into the corresponding stub location and adapt.

## Directory map

| Example | What it implements | Replaces stub at |
|---------|--------------------|------------------|
| `servers/repl/core.py` | Lean REPL subprocess with non-blocking I/O, import caching, memory monitoring, auto-restart | `servers/repl/core.py` |
| `servers/repl/pool.py` | Thread pool with queue-based dispatch, staggered startup, memory monitoring | `servers/repl/pool.py` |
| `servers/repl/server.py` | FastMCP server wiring for the REPL pool | `servers/repl/server.py` |
| `servers/mathlib/core.py` | Ripgrep-based Mathlib search, name finder, file reader | `servers/mathlib/core.py` |
| `servers/mathlib/server.py` | FastMCP server wiring for Mathlib search | `servers/mathlib/server.py` |
| `servers/lsp/server.py` | Lean LSP session (JSON-RPC), diagnostics, hover | `servers/lsp/server.py` |
| `servers/trace/core.py` | Append-only JSONL trace store with filtering and summaries | `servers/trace/core.py` |
| `servers/trace/server.py` | FastMCP server wiring for trace recording and querying | `servers/trace/server.py` |
| `servers/aristotle/server.py` | AristotleManager with multi-session support, polling, steering | `servers/aristotle/server.py` |
| `skills/autoform/SKILL.md` | Complete Mathlib conventions (full tactic tables, pitfall lists, simp rules) | `skills/autoform/SKILL.md` |
| `skills/autoform-prove/SKILL.md` | Full proof strategies and workflow | `skills/autoform-prove/SKILL.md` |
| `skills/autoform-review/SKILL.md` | Complete review checklist | `skills/autoform-review/SKILL.md` |
| `skills/autoform-quality/SKILL.md` | Full quality inspection rules | `skills/autoform-quality/SKILL.md` |
| `skills/autoform-extract/SKILL.md` | Complete extraction workflow | `skills/autoform-extract/SKILL.md` |
| `skills/autoform-crew/SKILL.md` | Full crew orchestration protocol | `skills/autoform-crew/SKILL.md` |
| `agents/autoform-worker.md` | Rich worker prompt with 5-step workflow, rules, integrity checks | `agents/autoform-worker.md` |
| `agents/autoform-reviewer.md` | Full reviewer prompt with 6-point checklist | `agents/autoform-reviewer.md` |
| `agents/autoform-reader.md` | Complete reader prompt with reading strategies | `agents/autoform-reader.md` |

## How to use

1. Find the stub you want to implement in the table above.
2. Copy the example file to the stub location:
   ```bash
   cp examples/servers/repl/core.py servers/repl/core.py
   ```
3. Read through the example and adapt it to your needs. The examples are designed to work as-is, but you may want to adjust configuration defaults, error messages, or timeouts.
4. Run the smoke tests to verify everything still imports and creates correctly:
   ```bash
   pytest tests/
   ```
5. If you changed any server wiring, test the server standalone:
   ```bash
   python -m servers.repl
   ```
