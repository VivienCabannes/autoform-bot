# Sorry Handling

The minimum bar for acceptance is net sorry reduction. Count sorrys before and after: `grep -c "sorry" file.lean`.

## Rules

- Never decompose a single sorry into multiple sorry'd helpers — reviewers reject if sorry count increases.
- Never redistribute sorry by creating sorry'd helpers and proving the target from them — `lean_verify` detects `sorryAx` in the axiom list.
- Never introduce a new sorry to close an existing one. If changing a definition breaks other proofs, fix ALL of them.
- If you can't prove a helper, inline the attempt instead of leaving it as a separate sorry'd lemma.
- Leaving a sorry as-is is always better than shuffling it around.
