---
description: Build the reviewer packet for a Lean change — spec sheet, kernel evidence, faithfulness scorecard — so a human expert verifies statements, not slop.
argument-hint: "[<ref> | --staged | --pr N | --decl NAME] [--book-dir DIR] [--score | --quick] [--verify ISSUE | --reject ISSUE]"
allowed-tools: Read, Bash, Grep, Glob, Task
---

# /autoform:review — the human-reviewer gate

Human Lean experts have minutes, not hours. Their job is to check the **trust surface** — that
the *statements* mean what they claim — because the kernel already checks the proofs. This
command builds the artifact that makes that possible: the **reviewer packet**. Arguments:
`$ARGUMENTS`. Read-only on the Lean code.

Load **formalization-workflow** (→ reviewer-packet) and **eval-rubrics**.

## Scope resolution

Echo what you are reviewing: a git ref (`git diff <ref>`), `--staged`, a PR (`gh pr diff N`), a
single declaration (`--decl NAME` — review its whole file section), or the working tree by
default. Identify every added/changed declaration in scope.

For `--pr N`, kernel evidence needs the PR's code on disk: `git fetch origin pull/N/head`,
then `git worktree add .review-pr-N FETCH_HEAD`, run all checks there, and
`git worktree remove .review-pr-N` afterwards. This does not count as a write to the code.

**Source location:** the informal source defaults to auto-detect (a `book.md`/`.tex`, an
`autoform-plan.yaml`, or an axiom ledger in the repo); `--book-dir DIR` overrides. Without a
locatable source, the spec sheet falls back to docstring/plan citations — and says so.

## The reviewer packet (always produced)

Canonical structure and rules: **formalization-workflow → reviewer-packet** (spec sheet with
faithfulness argument → kernel evidence → verdict → reading guide). Operationally:

**1. Spec sheet — the trust surface.** For each new/changed declaration, a row: Lean statement
(verbatim signature) · source statement (verbatim) · plain-math meaning (one sentence) ·
source citation · trust class:
- `DEF` / `STMT` — definitions and statement signatures: **a human must read these**;
- `INSTANCE` / `NOTATION` — new instances, notation, coercions, `@[simp]` attributes:
  must-read (they change what statements mean);
- `PROOF` — proof bodies, including helper lemmas consumed only by the target proof:
  kernel-checked, skim only (list which declarations were bucketed here);
- `AXIOM` / `SORRY` — gaps: each one justified (or ledger-matched) or the packet fails.
Order rows so the few lines a human *must* read come first, and say how many lines that is.

**2. Kernel evidence — mechanical, not vibes.** Run, do not infer:
- `lake env lean` on each changed file (or confirm a green build);
- `#print axioms <decl>` for every declaration in scope — report the axiom *delta* vs base
  (expected: `propext`, `Classical.choice`, `Quot.sound`; in audited-ledger repos each extra
  axiom is matched against its ledger entry; anything else is called out);
- for axiom discharges: the statement delta (`git diff -U0` — only `axiom` → `theorem`, type
  byte-identical);
- word-boundary grep for `sorry`/`admit`/`axiom` introduced by this change (`.lean` files
  only, comment lines filtered — a backstop to `#print axioms`, not the evidence);
- any soundness scripts the repo's CI runs (axiom reports, count consistency), via
  `env -u ANTHROPIC_API_KEY`.

**3. Verdict.** Dispatch in parallel: **code-reviewer** (faithful to source? cheating patterns —
trivial substitution, smuggled hypotheses, weakened conclusions, proxy objects?) and
**quality-inspector** (Mathlib idiom only). Overall `APPROVED` only if both approve **and** the
kernel evidence is clean; otherwise `REJECTED` with a de-duplicated file:line issue list.

**4. Reading guide.** Close the packet with: the suggested 5-minute review path, spot-checks
worth doing (e.g. "compare Def 2.1 against book p. 34 — quantifier order"), and red flags that
were checked and cleared.

## Scoring modes

- `--score` — full jury: **matcher** resolves each informal statement to its Lean declaration,
  then one **judge** per active rubric (faithfulness, proof_integrity, code_quality) per
  declaration; weighted scorecard with pass thresholds per eval-rubrics. Requires a locatable
  source (see Source location above); without one, score only proof_integrity/code_quality and
  state the degradation in the scorecard.
- `--quick` — the 7-dimension rater (one subagent, one-line JSON per eval-rubrics →
  seven-dimension-rater) for a fast diagnosis. No jury.

## Issue-tracked review (repos that gate merges on human sign-off)

If the repo tracks per-declaration review via GitHub issues (a `review:` label convention or
`.marathon/review/` config):
- `--verify ISSUE` — mark verified, close the sub-issue, and merge its PR if the repo's
  convention says verified ⇒ merge (`gh pr merge --merge --delete-branch`).
- `--reject ISSUE` — record the rejection with the packet's issue list as the comment, so the
  fix loop picks it up.

These two are the only write actions this command may take, and only when explicitly passed.

## Required artifact

The packet, in order: spec sheet → kernel evidence → verdict → reading guide (→ scorecard when
requested). If any piece is missing — say so plainly rather than approving on partial evidence.
