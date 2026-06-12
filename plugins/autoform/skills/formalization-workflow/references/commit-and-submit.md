# Commit and Submit Strategy

**Precedence:** in audited-axiom-ledger repos (see `axiom-discharge.md`), "commit early with
sorries" does not apply to the axiom layer — a discharge lands as one sorry-free commit with
the ledger and machine reports updated together.

Commit early and report back frequently. Don't exhaust your effort budget perfecting proofs.

## Commit

- Commit your first compiling change early, rather than holding everything for one big drop.
- Once the file compiles (even with a tracked sorry, where the project's policy allows it), commit.
- One logical step per commit — smaller diffs are easier to review and merge.
- When your effort budget is nearly spent, stop and report state honestly rather than cleaning up cosmetics.

## Report back

- When diagnostics show 0 errors, commit and report to the coordinating session immediately. Warnings (unused variables, deprecated names) don't block acceptance.
- Don't keep iterating after the build confirms correctness.
- Partial progress (fewer sorries, better structure) is worth reporting — with an honest gap list, never disguised as done.
