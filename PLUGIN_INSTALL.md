# AutoForm Plugin — Install Guide

This repository ships **one source** with three harness manifests. No build step — install directly from the repository root.

## 1) Muse / TBH (native, primary)

Source manifest: `.muse-plugin/plugin.json` (schemaVersion 1, `compat.manifestDir: .muse-plugin`)

```bash
cd /path/to/autoform-bot

# validate, install, enable directly from repo root
tbh plugins validate . --json
tbh plugins install .
tbh plugins enable autoform

# verify
tbh plugins list | grep autoform
```

To reinstall after edits: `tbh plugins install .` again (with `--force` if needed).

Skills exposed: `setup` (create/repair `blueprint/nodes/**/*.md`), `orchestrate` (work ready nodes), and `review` (judge faithfulness, proof integrity, and Mathlib quality). MCP servers: `autoform-lsp` and `autoform-repl` (both require absolute `project_dir`).

## 2) Claude Code

Manifests: `.claude-plugin/plugin.json` + `.claude-plugin/marketplace.json` (marketplace name `autoform`, plugin `autoform` at `./`)

```text
# Run these slash commands from Claude Code opened at the repository root:
/plugin marketplace add ./
/plugin install autoform@autoform
/plugin list
```

MCP servers run via `uv run --project ${CLAUDE_PLUGIN_ROOT} python -m servers.lsp.server` / `servers.repl.server`.

## 3) Codex

Manifest: `.codex-plugin/plugin.json` (points to `skills/` and `.mcp.json`)

```bash
codex plugins add ./
codex plugins list
```

---

## Quick checks

```bash
uv sync --extra dev --extra repl
uv run autoform check examples/blueprint   # graph validator
uv run autoform-visualize blueprint        # static graph.html
uv run pytest -q
```

## Troubleshooting

- `tbh: command not found` → install `tbh` CLI first.
- `chmod 0700 /var/lib/tbh/...: Read-only file system` → you are in a sandboxed container; run the `tbh plugins ...` steps on your real dev machine, not inside the sandbox.
- After changing `.muse-plugin/plugin.json`, `skills/`, `servers/`, `autoform/`, or `visualization/`, re-run `tbh plugins install .`.
