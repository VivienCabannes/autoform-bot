# Tool Usage

Use the right tool for the job. Understand tool limitations.

## run_lean_code (REPL)

- Only has access to `import Mathlib` — cannot see custom project definitions.
- For custom defs, either copy them into the snippet or use `lean_verify` on the actual file.
- Use heavily (20+ times) to prototype: test fragments, inspect types with `#check`, build up proofs incrementally.

## lean_verify vs lake build

- `lean_verify` checks a single declaration — fast and reliable.
- `lake build` builds the full project — slow and can timeout on large files.
- Prefer `lean_verify` for iterative development. Only use `lake build` for final submission.

## Bash restrictions

- No semicolons (use `&&` for chaining).
- No input redirects (`<`).
- No newlines in commands (write multi-line scripts to `.py` then run).
- `rm` is forbidden — use `file_delete` tool.
