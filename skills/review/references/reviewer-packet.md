---
name: reviewer-packet
description: Structure and rules for the DAG-native reviewer packet — spec sheet, kernel evidence, jury scorecard — so a human verifies statements, not slop.
---

# The reviewer packet — respect the human's time

A formalization is only useful if a human Lean expert will vouch for it, and experts
will not read thousands of lines of generated proof. The packet's job is to shrink
the human's task to its irreducible core: **read the statements; let the kernel vouch
for the proofs.** In this plugin a packet is built **per node** (keyed by node `id`)
over the tiered plan, and a tier-1 **review deck** is just the bundle of its
children's packets plus the cluster roll-up.

## Why statements are the trust surface

The Lean kernel verifies that proofs prove their statements. It cannot verify that a
statement *means* what the informal source claims. So a human must read:

- every **definition** (a wrong definition silently changes every theorem using it);
- every **theorem signature** (hypotheses, conclusion, quantifier structure,
  coercions);
- every **axiom** and the justification for its existence;
- every new **instance** (incoherent diamonds make statements unintended) and every
  new **notation/macro/coercion/`@[simp]` attribute** (they change what displayed
  statements mean);
- nothing else, unless a red flag earns it a look.

**Helper-lemma rule:** helper lemmas consumed only by the target proof are
PROOF-class — the kernel vouches that they suffice; they are *not* must-read.
Must-read is: target statements, new definitions, axioms, instances, notation, and
intentionally exported API. The packet lists which declarations were bucketed as
helpers so the reviewer can audit the bucketing itself.

## Packet structure (per node)

1. **Spec sheet** — one row per new/changed declaration, must-read rows first:
   `Lean signature (verbatim)` · `source statement (verbatim, from the node's
   source_refs)` · `plain-math meaning (one sentence)` · `source citation` · `trust
   class (DEF / STMT / INSTANCE / NOTATION / PROOF / AXIOM / SORRY)`. The verbatim
   source column is what makes side-by-side checking possible without opening the
   book. State the total must-read line count up front ("a reviewer must read 14
   lines; the other 700 are kernel-checked proof bodies").

   The node carries the link: its `mathlib_declarations` name the Lean decl(s), its
   `source_refs` give the verbatim source citation, and `informal_content/<id>.md`
   (or the built blueprint env) gives the paraphrased meaning. The node **is** the
   informal statement — do not re-derive the node↔decl link.

2. **Kernel evidence** — paste real command output, never a summary of what you
   expect:
   - `lake env lean <file>` per changed file (or confirm a green build);
   - `#print axioms <decl>` per declaration, as a **delta vs base** — expected
     baseline is `propext`, `Classical.choice`, `Quot.sound`; in
     audited-axiom-ledger repos, additionally the ledgered axioms (cross-check each
     against its ledger entry); anything else is named and justified;
   - for an axiom discharge: the **statement delta** — `git diff -U0` on the
     declaration, which must show only the `axiom` → `theorem … := …` change with the
     type byte-identical;
   - a project-wide grep for introduced `sorry` / `admit` / `axiom` (word-boundary,
     `.lean` files only, comment lines filtered — a heuristic backstop to
     `#print axioms`, not the evidence);
   - whatever soundness scripts the repo's CI runs.

   If a `kernel/<id>.txt` dump exists next to the graph, include it verbatim. The
   review surface reads it read-only; **verification stays independent of the
   producer** — the worker never self-certifies.

3. **Jury scorecard** — the three blind single-axis judges from the `eval-rubrics`
   skill, read from the sidecar's `ai` slot for this node:
   - `faithfulness` (weight 0.40, pass ≥4) — Lean *statement* vs source;
   - `proof_integrity` (0.40, ≥3) — is the *proof* genuine;
   - `code_quality` (0.20, ≥3) — Mathlib idiom only.

   Displayed score = `0.40·faith + 0.40·integ + 0.20·qual`. Verdict is
   **threshold-gated, not the average**: **clean** = all pass; **rejected** = faith
   ≤2 **or** integ ≤2; **flagged** = otherwise. Style alone never rejects. Show the
   effective verdict (human if present, else ai) and its source.

4. **Faithfulness argument** — for each must-read row, *why* this Lean statement is
   the right rendering of the source: where each hypothesis comes from, why any extra
   hypothesis is provably redundant (or flagged), what the quantifiers range over,
   which Mathlib notion was chosen and why it matches the source's (e.g. "the
   chapter's 'smooth' = `ContMDiff ℂ ω` here because it works in the analytic
   category").

5. **Reading guide** — a 5-minute path: which rows to read in which order, 2–3
   suggested spot-checks against the source ("compare quantifier order in Def 2.1 vs
   the source's §2"), and the cheating patterns you checked and *cleared* (trivial
   substitution, smuggled hypotheses, weakened conclusion, proxy objects,
   `Prop`-encoded theorems, theorem-smuggling structure fields, incoherent instances,
   meaning-shifting notation).

## Cluster review decks

A tier-1 **review deck** = the packets of the cluster's tier-2 children plus the
**roll-up**: a cluster is clean **only if every** child is clean; any flagged or
rejected child ⇒ the cluster is flagged. The deck is the cluster drill-down screen;
it is assembled from node packets, not new infrastructure. Build specs bottom-up
(t2/t3 first), then assemble by cluster.

## Taint and the trust frontier

These are computed live by `review_model.py`, never stored:

- **Taint** — a flagged/rejected node taints (hatches) its entire downstream
  `depends_on` closure. A tainted node's own verdict may be clean, but it rests on
  something untrustworthy, so it is not yet vouchable.
- **Trust frontier** — the sink nodes whose entire `depends_on` closure is fully
  clean. Completeness is *this readout* (a target is "reached" when its closure is
  clean), not a separate check.

## Spec-first discipline

The packet is cheap when specs were gated *before* proving (the spec sheet already
exists). Write statements first; jury them against the source; only then spend proof
effort. A perfect proof of an unfaithful statement is wasted work — and the reviewer,
not the kernel, is the one who would have caught it.

## Rules

- Never claim "compiles" / "axiom-clean" without the command output in the packet.
- A packet with an unexplained `AXIOM`/`SORRY` row is a **failed** packet — say so.
- Keep the packet under ~2 pages; link or path-reference everything else.
- The review surface writes **only** `review_status.json`; the spec sheet, kernel
  evidence, and informalization are all read from pristine sources.
