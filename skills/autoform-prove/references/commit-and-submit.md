# Commit and submit — the honesty core

**Precedence:** in audited-axiom-ledger repos (see the *Axiom-discharge repos* section of
`axiom-policy.md`), "commit early with a tracked `sorry`" does **not** apply to the axiom layer —
a discharge lands as one sorry-free commit with the ledger and machine report updated together.

Commit atomically and report back honestly. This is the persistence twin of the FAILED rule
(`sorry-handling.md`): the worker's value is in what it *truthfully* delivers, not in how
finished it can make a partial result look.

## Commit

- Commit your first **compiling** change early, rather than holding everything for one big drop.
- Once the file compiles (even with a tracked `sorry`, where the project's policy allows it),
  commit. **One logical step per commit** — smaller, scoped diffs review and merge cleanly.
- When the effort budget is nearly spent, stop and commit the honest state rather than burning it
  on cosmetics.

## Report back — gap-listed, never disguised

- When diagnostics show 0 errors, commit and report immediately. Warnings (unused variables,
  deprecated names) don't block acceptance.
- Don't keep iterating after the build confirms correctness.
- **Partial progress is worth reporting — with an explicit gap list, never disguised as done.**
  Fewer `sorry`s, a cleaner structure, a proved helper: all real, all reportable. But say what is
  *still open* — every remaining `sorry`, every stubbed helper, every `unproved` placeholder —
  in plain terms. A commit that hides its gaps is the same cheat as a `FAILED` task delivered as
  "done": it poisons the reviewer's trust and everything downstream of the node.
