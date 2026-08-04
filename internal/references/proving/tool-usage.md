# Tool usage (REPL, LSP, mathlib search) and the build-timeout playbook

Use the right tool for the job, and know each tool's limits. Direct
`lake env lean` checks are the portable baseline across Claude, Codex, and
headless workers. The bundled REPL/LSP servers are optional accelerators; use
them when healthy and otherwise use the direct commands below.

## `autoform-repl` MCP — `run_lean_code`, `get_repl_status`

- Always pass the absolute Lake root as `project_dir`; it is required and is
  never inferred from the MCP process directory.
- `run_lean_code(project_dir, code, timeout?)` sends a snippet to a pooled Lean REPL and returns formatted
  diagnostics (status, errors, `sorry` goals, warnings). Imports are cached, so repeated calls
  with the same imports reuse the environment and stay fast.
- The REPL environment has **only `import Mathlib`** — it cannot see your project's custom
  definitions. For a fragment that references custom defs, paste those defs into the snippet, or
  check the actual file with the LSP instead.
- Use it to prototype: test fragments, inspect types with `#check`, probe for lemmas
  with `exact?` / `apply?` / `rw?`, and build a proof up incrementally — only write to the file
  once a fragment compiles.
- `get_repl_status(project_dir)` reports pool capacity, memory, and shutdown state.

## `autoform-lsp` MCP — `lean_diagnostic_messages`, `lean_hover`

- `lean_diagnostic_messages(project_dir, file_path)` returns the language server's errors/warnings/info for a
  real `.lean` file in the project — this is how you check a file that uses custom definitions
  the REPL can't see.
- `lean_hover(project_dir, file_path, line, character)` (0-indexed) gives the
  type/info at a position; use it to confirm what an expression elaborates to.

## Local Mathlib search — search before proving

- Resolve the project's real Mathlib checkout, usually
  `.lake/packages/mathlib`, and search it with `rg` before reproving a result.
  Read promising Lean source and validate the exact declaration with `#check`
  or a small compiling example.
- Host-native Lean or community search may supplement the local checkout, but
  do not claim a declaration exists until its source or Lean has confirmed it.

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
  placed **before** the declaration. Prototype in a standalone snippet via `run_lean_code` rather
  than rebuilding a large file. Once the LSP confirms the declaration, submit — don't keep
  iterating against a timeout you can't fix.

## Bash restrictions (when shelling out)

- No semicolons (chain with `&&`). No input redirects (`<`). No newlines in commands — write a
  multi-line script to a `.py`/`.sh` file and run that. Prefer `lake env lean <file>` over a full
  `lake build` for speed.
