# Build Timeout

The infrastructure has a hard timeout for `lake build` on full projects. Large Lean files (>50KB, >1000 lines) can timeout even with correct proofs.

## Detection

- All failures say "timed out after Ns" — no actual Lean error messages.
- `lean_verify` on individual declarations succeeds.
- This is an infrastructure issue, not a proof error.

## Mitigation

- Use `lean_verify` for correctness checks (single declaration, fast). Avoid `lean_diagnostic_messages` and `lean_file_outline` on large files (they timeout too).
- Minimize file edits and don't add new imports — each change invalidates `.olean` cache.
- Use `set_option maxHeartbeats 400000` (or higher) for computationally heavy proofs.
- Submit early after LSP confirms proof is correct. Don't iterate further — timeout is not fixable by the worker.
- Prototype in standalone files via `run_lean_code` to avoid rebuilding large files.
