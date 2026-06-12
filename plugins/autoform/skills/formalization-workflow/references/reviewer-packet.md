# The reviewer packet — respect the human's time

A formalization is only useful if a human Lean expert will vouch for it, and experts will not
read thousands of lines of generated proof. The packet's job is to shrink the human's task to
its irreducible core: **read the statements; let the kernel vouch for the proofs.**

## Why statements are the trust surface

The Lean kernel verifies that proofs prove their statements. It cannot verify that a statement
*means* what the informal source claims. So a human must read:

- every **definition** (a wrong definition silently changes every theorem that uses it);
- every **theorem signature** (hypotheses, conclusion, quantifier structure, coercions);
- every **axiom** and the justification for its existence;
- every new **instance** (incoherent diamonds make statements unintended) and every new
  **notation/macro/coercion/`@[simp]` attribute** (they change what displayed statements mean);
- nothing else, unless a red flag earns it a look.

**Helper-lemma rule:** helper lemmas consumed only by the target proof are PROOF-class — the
kernel vouches that they suffice; they are *not* must-read. Must-read is: target statements,
new definitions, axioms, instances, notation, and intentionally exported API. The packet lists
which declarations were bucketed as helpers so the reviewer can audit the bucketing itself.

Everything in the packet is organized to make that reading fast and decisive.

## Packet structure

1. **Spec sheet** — one row per new/changed declaration, must-read rows first:
   `Lean signature (verbatim)` · `source statement (verbatim, from the book/ledger)` ·
   `plain-math meaning (one sentence)` · `source citation` · `trust class (DEF / STMT /
   INSTANCE / NOTATION / PROOF / AXIOM / SORRY)`. The verbatim source column is what makes
   side-by-side checking possible without opening the book. State the total must-read line
   count up front ("a reviewer must read 14 lines; the other 700 are kernel-checked proof
   bodies").

2. **Kernel evidence** — paste real command output, never a summary of what you expect:
   - `lake env lean <file>` per changed file;
   - `#print axioms <decl>` per declaration, as a **delta vs base** — expected baseline is
     `propext, Classical.choice, Quot.sound`; in audited-axiom-ledger repos, additionally the
     ledgered axioms (cross-check each against its ledger entry); anything else is named and
     justified;
   - for an axiom discharge: the **statement delta** — `git diff -U0` on the declaration,
     which must show only the `axiom` → `theorem … := …` change with the type byte-identical;
   - project-wide grep for introduced `sorry` / `admit` / `axiom` (word-boundary, `.lean` files
     only, comment lines filtered — a heuristic backstop to `#print axioms`, not the evidence);
   - whatever soundness scripts the repo's CI runs.

3. **Faithfulness argument** — for each must-read row, *why* this Lean statement is the right
   rendering of the source: where each hypothesis comes from, why any extra hypothesis is
   provably redundant (or flagged), what the quantifiers range over, which Mathlib notion was
   chosen and why it matches the book's (e.g. "book's 'smooth' = `ContMDiff ℂ ω` here because
   the chapter works in the analytic category").

4. **Verdict** — the gate outcome (code-reviewer × quality-inspector, APPROVED only if both
   approve and the kernel evidence is clean), with the de-duplicated file:line issue list when
   rejected.

5. **Reading guide** — a 5-minute path: which rows to read in which order, 2–3 suggested
   spot-checks against the source ("compare quantifier order in Def 2.1 vs book p. 34"), and
   the cheating patterns you checked and *cleared* (trivial substitution, smuggled hypotheses,
   weakened conclusion, proxy objects, `Prop`-encoded theorems, theorem-smuggling structure
   fields, incoherent instances, meaning-shifting notation).

## Spec-first discipline

The packet is cheap when specs were gated *before* proving (the spec sheet already exists).
Write statements first; jury them against the source; only then spend proof effort. A perfect
proof of an unfaithful statement is wasted work — and the reviewer, not the kernel, is the one
who would have caught it.

## Rules

- Never claim "compiles" / "axiom-clean" without the command output in the packet.
- A packet with an unexplained `AXIOM`/`SORRY` row is a **failed** packet — say so.
- Keep the packet under ~2 pages; link or path-reference everything else.
