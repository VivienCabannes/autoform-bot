# Contributing to Autoform

## Overview

Autoform is a **template plugin** — the wiring (manifests, hooks, commands, discovery files) is complete, but the domain content is shipped as stubs. Contributions fill in these stubs with real implementations. Each server, skill, and agent is independent, so you can contribute one without touching the others.

## What to work on

| Component | Location | Status | Difficulty | Notes |
|-----------|----------|--------|------------|-------|
| **Workspace server** | `servers/workspace/` | Implemented | — | Reference implementation; do not need to touch |
| **REPL server** | `servers/repl/` | Stub | Hard | Subprocess management, non-blocking I/O, memory monitoring |
| **Mathlib server** | `servers/mathlib/` | Stub | Medium | Ripgrep-based search; tools return "not implemented" |
| **LSP server** | `servers/lsp/` | Stub | Hard | JSON-RPC language server, Content-Length framing, diagnostics |
| **Trace server** | `servers/trace/` | Stub | Easy | JSONL append-only store; straightforward file I/O |
| **Aristotle server** | `servers/aristotle/` | Stub | Medium | Multi-session wrapper around `aristotlelib`; requires API key |
| **autoform skill** | `skills/autoform/SKILL.md` | Stub | Medium | Has section headings + some rules; needs full tactic tables, pitfall lists |
| **autoform-prove skill** | `skills/autoform-prove/SKILL.md` | Stub | Medium | Has workflow steps; needs full sorry/axiom/cheating details |
| **autoform-review skill** | `skills/autoform-review/SKILL.md` | Stub | Medium | Has checklist headings; needs full cheating detection patterns |
| **autoform-quality skill** | `skills/autoform-quality/SKILL.md` | Stub | Easy | Has style rules; needs full naming/tactic/code style details |
| **autoform-extract skill** | `skills/autoform-extract/SKILL.md` | Stub | Easy | Has process steps; needs full YAML examples and guidelines |
| **autoform-crew skill** | `skills/autoform-crew/SKILL.md` | Stub | Medium | Has agent table; needs full orchestration patterns and examples |
| **Worker agent** | `agents/autoform-worker.md` | Stub | Easy | Frontmatter correct; needs rich system prompt |
| **Reviewer agent** | `agents/autoform-reviewer.md` | Stub | Easy | Frontmatter correct; needs rich system prompt |
| **Reader agent** | `agents/autoform-reader.md` | Stub | Easy | Frontmatter correct; needs rich system prompt |

## How to contribute a server

The workspace server (`servers/workspace/`) is the reference implementation. Study its structure first.

1. **Read the reference.** Look at `servers/workspace/core.py` (pure logic, no MCP imports) and `servers/workspace/server.py` (FastMCP wrapper with `create_*_server()` factory and `__main__` block).

2. **Read the example.** The `examples/servers/<name>/` directory contains a full working implementation for the server you want to build. This is your primary reference.

3. **Implement `core.py`.** Write the pure logic. No `fastmcp` imports in this file — it should be testable without MCP dependencies. Keep the existing dataclasses and function signatures so that `server.py` does not need changes.

4. **Wire `server.py` tools.** The server file should already have the correct tool definitions calling into `core.py`. If you change core function signatures, update the server wiring to match.

5. **Run tests.**
   ```bash
   pytest tests/test_servers.py
   ```
   The smoke tests verify that each server module imports without error and that `create_*_server()` returns a valid FastMCP instance.

6. **Test standalone.**
   ```bash
   python -m servers.<name>
   ```
   This starts the MCP server on stdio. You can test it with any MCP client.

## How to contribute a skill

Skills are Markdown files with YAML frontmatter. Each skill is a self-contained reference document that the AI assistant loads into context when triggered.

1. **Read the example.** The `examples/skills/<name>/SKILL.md` contains the full reference version. Compare it with the stub at `skills/<name>/SKILL.md` to see what is missing.

2. **Fill in the `<!-- TODO -->` sections.** Each TODO comment describes what content belongs there and points to the example file for reference.

3. **Keep TODO markers** for sections you do not finish. This lets others pick up where you left off.

4. **Preserve the YAML frontmatter.** The `name`, `description`, and trigger patterns must stay unchanged — they control when the skill is loaded.

5. **Create or update `README.md`.** Each skill directory has a `README.md` for humans. Keep it in sync with the SKILL.md content.

## How to contribute an agent prompt

Agent prompts are Markdown files with YAML frontmatter that defines the agent's tools, MCP servers, and model.

1. **Read the example.** The `examples/agents/<name>.md` contains the full reference version. Compare it with the stub at `agents/<name>.md`.

2. **Preserve the frontmatter.** The `name`, `tools`, `mcpServers`, and `model` fields are correct in the stubs and must not change.

3. **Expand the body.** Replace the `<!-- TODO -->` comments with concrete workflow steps, rules, integrity checks, and output specifications. The agent prompt should be specific enough that the model follows the workflow without ambiguity.

4. **Keep it self-contained.** Agent prompts should not reference external files. Everything the model needs should be in the prompt itself (skills are loaded separately).

## Testing

Run the smoke tests from the repo root:

```bash
pytest tests/
```

The test suite checks:
- Every server module imports without error
- Every `create_*_server()` function returns a FastMCP instance
- The workspace server's `inspect_workspace()` returns a dict with expected keys

## Style

- **Python:** Follow `ruff` with 120-character line length. Run `ruff check servers/` before submitting.
- **Markdown:** YAML frontmatter is required for all skills and agents. Follow the existing section structure.
- **Follow existing patterns.** When in doubt, look at the workspace server (for Python) or the autoform-prove skill (for Markdown).

## PR guidelines

- **One stub at a time.** Each PR should fill in one server, one skill, or one agent. This keeps reviews focused.
- **Include tests.** If you add a new server, add corresponding tests in `tests/test_servers.py`. If you change existing server APIs, update the existing tests.
- **Run the full test suite** before submitting: `pytest tests/`.
- **Reference the example.** In your PR description, note which `examples/` file you used as reference.
