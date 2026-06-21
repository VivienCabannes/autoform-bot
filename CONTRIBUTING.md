# Contributing to Autoform

## Overview

Autoform is a **template plugin** — the wiring (manifests, hooks, discovery files) is complete, but domain content is added via PRs. Each server, skill, and agent is independent, so you can contribute one without touching the others.

## What to work on

### Servers (stub → implementation)

| Component | Location | Status | Difficulty | Notes |
|-----------|----------|--------|------------|-------|
| **Zulip server** | `servers/zulip/` | ✅ Implemented | — | Wraps `skills/zulip/zulip-search.py` |
| **REPL server** | `servers/repl/` | ⬜ Stub | Hard | Subprocess management, non-blocking I/O, memory monitoring |
| **LSP server** | `servers/lsp/` | ⬜ Stub | Hard | JSON-RPC language server, Content-Length framing, diagnostics |
| **Aristotle server** | `servers/aristotle/` | ⬜ Stub | Medium | Multi-session wrapper around `aristotlelib`; requires API key |

### Skills (new)

These skills don't exist yet. Create them from scratch — see `examples/skills/` for reference content.

| Skill | Suggested location | Difficulty | What it should cover |
|-------|--------------------|------------|---------------------|
| **Mathlib conventions** | `skills/autoform/` | Medium | Lean 4 + Mathlib style, tactics, naming, pitfalls |
| **Proof strategies** | `skills/autoform-prove/` | Medium | Incremental proving, REPL prototyping, sorry/axiom handling |
| **Statement extraction** | `skills/autoform-extract/` | Easy | Extract formalizable statements from LaTeX/Markdown to YAML |
| **Crew orchestration** | `skills/autoform-crew/` | Medium | When and how to spawn worker/reviewer/reader subagents |

### Agents (stub → rich prompt)

| Component | Location | Status | Difficulty | Notes |
|-----------|----------|--------|------------|-------|
| **Worker agent** | `agents/autoform-worker.md` | ⬜ Stub | Easy | Frontmatter correct; needs rich system prompt |
| **Reviewer agent** | `agents/faithfulness-reviewer.md` | ⬜ Stub | Easy | Frontmatter correct; needs rich system prompt |
| **Reader agent** | `agents/autoform-reader.md` | ⬜ Stub | Easy | Frontmatter correct; needs rich system prompt |

## How to contribute a server

The zulip server (`servers/zulip/`) is the reference implementation. Study its structure first.

1. **Read the reference.** Look at `servers/zulip/server.py` (FastMCP wrapper importing from a skill script) and `skills/zulip/zulip-search.py` (pure logic). For stubs, the server file already has tool definitions that return "not implemented".

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
   uv run python -m servers.<name>
   ```
   This starts the MCP server on stdio. You can test it with any MCP client.

## How to contribute a skill

Skills are Markdown files with YAML frontmatter. Each skill is a self-contained reference document that the AI assistant loads into context when triggered.

1. **Create the directory.** `skills/<name>/SKILL.md` with YAML frontmatter (`name`, `description` with trigger patterns).

2. **Check examples.** The `examples/skills/<name>/SKILL.md` may contain a reference version.

3. **Add scripts if needed.** If the skill automates a task, put the script alongside the SKILL.md (e.g. `skills/<name>/<name>.sh`). Add a case to `hooks/user-prompt-submit` to auto-run it.

4. **Preserve the YAML frontmatter.** The `name`, `description`, and trigger patterns control when the skill is loaded.

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
- The zulip skill's config discovery and CLI work correctly

## Style

- **Python:** Follow `ruff` with 120-character line length. Run `ruff check servers/` before submitting.
- **Markdown:** YAML frontmatter is required for all skills and agents. Follow the existing section structure.
- **Follow existing patterns.** When in doubt, look at the zulip server (for Python) or `skills/install-lean/` (for skills with scripts).

## PR guidelines

- **One component at a time.** Each PR should add one server, one skill, or one agent. This keeps reviews focused.
- **Include tests.** If you add a new server, add corresponding tests in `tests/test_servers.py`. If you change existing server APIs, update the existing tests.
- **Run the full test suite** before submitting: `pytest tests/`.
- **Reference the example.** In your PR description, note which `examples/` file you used as reference.
