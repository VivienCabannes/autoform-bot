# Plugin smoke test — repeatable end-to-end eval

`plugin-lint` (CI) proves the artifacts are well-formed. It cannot prove the
commands *behave* well — that needs a real run. This is the documented,
repeatable procedure (no committed harness, because each step is a live Claude
Code session billed to the Max subscription).

## Prerequisites

- A Lean 4 / Mathlib project with at least one open goal — ideally a repo with an
  **audited axiom ledger** (e.g. a clone of `mrdouglasny/jacobian-challenge`),
  since that exercises the discharge path and the ledger-aware checks.
- The plugin installed: `/plugin marketplace add <repo>` then
  `/plugin install autoform@autoform-suite`.
- `lake` on PATH; a warm build (`lake build` green) so `lake env lean` is fast.

## A. Prove mode — discharge / fill a goal (the headline path)

1. Pick a target: a `sorry`, an `axiom AX_*`, or a named declaration.
2. `/autoform:run <target> --dry-run` — **expect:** a spec note + lemma plan, an
   independent `judge` faithfulness check on the *statement*, and **no edits**.
3. `/autoform:run <target>` — **expect:** worker writes the proof; Phase 4 prints
   real `lake env lean` + `#print axioms` output; the review gate runs; a reviewer
   packet is emitted. **Pass criteria:** statement byte-identical to the source
   (for a discharge), zero `sorryAx`, only standard-3 axioms *plus* ledgered ones,
   no new `sorry`/`axiom` anywhere.
4. Confirm no API billing: the run used only in-session subagents (no
   `--aristotle` / `--engine python`, no unscrubbed `claude -p`).

## B. Review mode — the reviewer packet

5. `/autoform:review <ref|--decl NAME>` — **expect:** spec sheet with the verbatim
   source statement beside each Lean signature, kernel evidence (axiom delta vs
   ledger; statement delta for a discharge), an APPROVED/REJECTED verdict, and a
   5-minute reading guide. **Pass criteria:** a Lean expert can reach a verdict
   from the packet alone, without opening the proof bodies.

## C. Formalize mode — informal source (optional, slower)

6. `/autoform:run path/to/book.md --scope "ch. 1" --spec-only` — **expect:**
   `autoform-plan.yaml` (dependency waves + Mathlib status) and a spec file gated
   by the faithfulness jury; stops before proving. **Pass criteria:** every
   statement carries a source citation; flagged statements are surfaced, not
   silently formalized.

## Reference run (2026-06)

The `AX_ofCurve_contMDiff` discharge in `mrdouglasny/jacobian-challenge` was
verified via path B entirely on subscription: the packet confirmed the proof
genuine and the statement byte-identical, and flagged the missing ledger/report
bookkeeping — which is exactly the catch a human reviewer would otherwise have to
make. (Merged upstream as that repo's PR #179.)
