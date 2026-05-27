# Commit and Submit Strategy

Commit early and submit builds frequently. Don't exhaust turns perfecting proofs.

## Commit

- Commit your first change within 20 turns, even if just converting one axiom to `theorem ... := by sorry`.
- Once the file compiles (even with sorry), commit immediately.
- One sorry per commit when possible — smaller diffs are easier to review and merge.
- Reserve the last 20 turns for submission. At turn 230, submit now rather than cleaning up.

## Submit

- When `lean_diagnostic_messages` shows 0 errors, submit immediately. Warnings (unused variables, deprecated names) don't block acceptance.
- Don't iterate after LSP confirms correctness — submit the build request.
- Partial progress (fewer sorrys, better structure) is always worth submitting.
