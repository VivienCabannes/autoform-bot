# Tool usage (REPL, LSP, mathlib search) and the build-timeout playbook

Use the right tool for the job, and know each tool's limits. Direct
`lake env lean` checks are the portable baseline across Claude, Codex, and
headless workers. Autoform registers the real `lean-lsp-mcp` because proof
state and language-server caches are stateful. Stateless search uses ordinary
CLI and HTTP tools instead of resident MCP processes.

## `lean-lsp-mcp` (stateful)

- `lean_goal` inspects goals before and after a tactic line. Use it often while
  constructing a proof.
- `lean_diagnostic_messages` checks the real project file, including custom
  definitions and imports.
- `lean_hover_info` and `lean_code_actions` expose types, documentation, and
  resolved `Try this` edits.
- `lean_multi_attempt` tries several tactics against one proof state without
  changing the file.
- `lean_run_code` checks a self-contained snippet with its own imports.
- `lean_verify` checks theorem axioms. The deterministic verification gate is
  still authoritative for accepting a proof.

## Stateless search

- Use `loogle '<query>'` for declaration names, subexpressions, and type shapes.
- Use `lean-explore search '<description>'` for semantic natural-language search.
- Use `python3 <plugin>/scripts/mathlib_search.py {name|grep|read|path} ...`
  when results must be verified against the project's local Mathlib checkout.
- Use Lean-native `exact?`, `apply?`, `simp?`, and `rw?` inside the project.
- Use the Zulip Python API only when community discussion or naming history is
  relevant; follow `internal/runbooks/zulip.md`.

## `lean_diagnostic_messages` / a single file vs full `lake build`

- Checking the one file you changed (via the LSP) or a single declaration (via the REPL) is fast
  and reliable. Prefer that for iterative development.
- `lake build` builds the full project — slow, and it can time out on large files. Reserve it
  for a final, pre-submission check.

## Build-timeout playbook (folded in from build-performance)

The infrastructure has a hard timeout for `lake build` on full projects. A large file (>50 KB,
>1000 lines) can time out even with correct proofs.

- **Symptom:** failures say "timed out after Ns" with no actual Lean error, while a single
  declaration checks fine in isolation. That is an infrastructure limit, not a proof error.
- **Mitigation:** check single declarations (REPL) or the one changed file (LSP) instead of
  re-running `lake build`; avoid full-file diagnostics on huge files (they time out too).
  Minimize edits and avoid adding imports — each change invalidates the `.olean` cache for the
  file and its dependents. Use `set_option maxHeartbeats 400000` (or higher) for heavy proofs,
  placed **before** the declaration. Prototype in a standalone snippet via `lean_run_code` rather
  than rebuilding a large file. Once the LSP confirms the declaration, submit — don't keep
  iterating against a timeout you can't fix.

## Bash restrictions (when shelling out)

- No semicolons (chain with `&&`). No input redirects (`<`). No newlines in commands — write a
  multi-line script to a `.py`/`.sh` file and run that. Prefer `lake env lean <file>` over a full
  `lake build` for speed.
