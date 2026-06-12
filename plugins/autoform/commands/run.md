---
description: Point autoform at a sorry/axiom or an informal text — it builds and runs the right formalization workflow (spec-gate first, subscription-billed).
argument-hint: "<file.lean[:line] | decl | axiom | book.md/.tex/.pdf> [--spec-only] [--dry-run] [--aristotle] [--engine python] [--review-cycles N] [--scope \"ch. N[-M]\"]"
allowed-tools: Read, Write, Edit, Bash, Grep, Glob, Task, Skill
---

# /autoform:run — one entry point, the whole pipeline

Take a target — a `sorry`/`axiom`/declaration in Lean code, **or** an informal textbook/paper —
and run a complete, honest formalization workflow against it: plan → **spec gate** → prove →
verify → reviewer packet. Arguments: `$ARGUMENTS`.

Load the **lean-conventions**, **formalization-workflow**, and **eval-rubrics** skills before
anything else.

## Cost policy (default: Claude subscription only)

Everything below runs in-session (you + Task subagents, all from the autoform plugin), billed
to the Claude subscription. Never let `ANTHROPIC_API_KEY` leak into subprocesses: run repo
scripts and any non-Claude child via `env -u ANTHROPIC_API_KEY …`, and scrub it from `claude -p`
children so keychain OAuth is used. Two opt-in paid paths exist and **must never be chosen
silently**:

- `--aristotle` — delegate proof search to Harmonic's Aristotle (separate `ARISTOTLE_API_KEY`
  billing). Use the `marathon` CLI if installed, else an autoform-bot checkout's aristotle
  backend — and note that backend's QC/steering legs can call the Anthropic API unless run
  with the key scrubbed (CLI/OAuth mode). If neither runner exists, say `--aristotle` is
  unavailable and continue in-session.
- `--engine python` — the legacy autoform-bot Python engine (`python -m autoform.bot.main` /
  `autoform.eval` / `autoform.statement_extraction`), pay-per-token via raw API keys. It
  replaces the in-session phases below (plan/prove/eval) wholesale; warn about billing before
  launching, and still apply Required artifacts to its output.

## Target detection

Resolve `$ARGUMENTS` and **echo your conclusion** before acting:

- **Prove mode** — the target is Lean: a `.lean` path (optionally `:line`), a declaration name,
  an axiom name (e.g. `AX_RiemannRoch`), or "the sorry in <file>". Grep the repo to locate it.
- **Formalize mode** — the target is informal: a `.md`/`.tex`/`.pdf` path or a directory
  holding one. The source must resolve to a readable file; if the user only *described* a
  source, ask for the file (or fetch it) before Phase 0 — the one sanctioned exception to "do
  not stall". If both a Lean repo and a book are given, the book names *what* to formalize and
  the repo is *where*.

If the target is ambiguous, say what you ruled out and pick the best reading — do not stall.

## Prove mode (sorry / axiom / open declaration)

**Phase 0 — Context.** Read the declaration and its file. Read the project's ground rules
(`CLAUDE.md`, `AGENTS.md`, contributor docs) and any per-target plan the repo keeps (e.g.
`docs/planning/`, audit ledgers like `AXIOM_AUDIT.md`). Confirm the build works before changing
anything (`lake env lean` on the untouched file; fix toolchain/cache first if not — see
formalization-workflow → build-timeout).

**Phase 1 — Spec gate.** Before any proof work, write a short **spec note**: the statement in
plain mathematics, its source (book/paper/ledger citation), and why the Lean statement is the
*right* formalization — no smuggled hypotheses, no vacuous quantifiers, satisfiable if it is an
axiom being discharged. If the statement itself is wrong, vacuous, or unfaithful, **stop and
report** — proving a bad statement is worse than proving nothing. For axiom-discharge repos,
follow formalization-workflow → axiom-discharge.

**Phase 2 — Strategy.** Search Mathlib first (`exact?`, `apply?`, `loogle`, LSP MCP tools, or
grepping a local Mathlib). Decompose the goal into named helper lemmas with full statements —
this lemma plan *is* the spec a human reviewer will read. For a sizable goal, dispatch 2–3
scouts in parallel (Task → general agents) to map distinct proof routes, then pick one.
**Independent spec check:** before any proof spend (and unconditionally before `--aristotle`),
have a **judge** (faithfulness rubric, the spec note + ledger/source as ground truth) confirm
the *statement* is the right one — the author of the spec note does not get to be its only
reviewer. With `--dry-run` or `--spec-only`, stop here: print the plan, and for `--spec-only`
emit the reviewer packet for the spec.

**Phase 3 — Prove.** Dispatch the autoform **worker** subagent with a self-contained task: the
spec note, the lemma plan, the file, and the no-cheating rules. Independent helper lemmas go to
parallel workers **with disjoint file ownership**; the session serializes merges and resolves
shared-file conflicts. Verify incrementally (`lake env lean <file>` after each lemma lands, not
only at the end). With `--aristotle`, send the prepared lemma plan to Aristotle instead and
review what comes back exactly as if a worker wrote it.

**Phase 4 — Verify (mechanical, non-negotiable).** Run the kernel-evidence checklist
(formalization-workflow → reviewer-packet §2): `lake env lean` on every touched file;
`#print axioms` on the target and every new helper — expected axioms (`propext`,
`Classical.choice`, `Quot.sound`) **plus, in audited-ledger repos, axioms matching ledger
entries** (cross-check each; zero `sorryAx`, zero unledgered axioms); for a discharge, the
statement delta (`git diff -U0`: only `axiom` → `theorem`, type byte-identical); the
project-wide sorry/axiom grep; and any repo soundness scripts CI runs (via
`env -u ANTHROPIC_API_KEY`).

**Phase 5 — Gate and packet.** Run the review gate (**code-reviewer** + **quality-inspector** in
parallel; loop rejections back to a fresh worker up to `--review-cycles`, default 2). Then emit
the **reviewer packet** (formalization-workflow → reviewer-packet) and commit following the
repo's convention. Open a PR only if the repo's rules are satisfied and the user asked for one.

## Formalize mode (textbook / paper)

**Phase 0 — Ingest.** Read the source (PDFs: read visually, ~20 pages per pass). Resolve scope
(`--scope "ch. 1-2"`, a section list, or all). Locate or `lake init` the target Lean repo.

**Phase 1 — Plan.** Dispatch the **planner** subagent over the scoped source to produce
`autoform-plan.yaml`: every definition/theorem/lemma with an id, source citation, LaTeX
statement, dependencies, and a Mathlib status (`exists` / `partial` / `missing`). For large
scopes, run planners per chapter in parallel and reconcile (dedupe by mathematical content, not
label). Statements whose prerequisites are all in Mathlib form wave 1; the rest order into
later waves. With `--dry-run`, stop here and print the plan summary.

**Phase 2 — Spec first.** Formalize **statements only** for the current wave: definitions in
full; theorem bodies are `sorry` placeholders, tracked in the plan for elimination in Phase 3
(or the project's sanctioned placeholder where the source omits the proof — if the project has
no such convention, pick/bootstrap one now, e.g. a tiny `unproved` macro in `<Lib>/Util/`).
Then gate the *statements* through a faithfulness jury, **declaring "spec stage" in every
dispatch**: **judge** subagents score each against the source (faithfulness rubric);
**code-reviewer** in spec-stage mode hunts cheating patterns. Fix or flag every score < 4. The
surviving spec file is the artifact a human reviewer signs off on. With `--spec-only`, stop
here and emit the reviewer packet for the spec.

**Phase 3 — Prove.** Work through the approved spec in dependency order: one autoform
**worker** per target, independent targets in parallel waves **with disjoint file ownership**;
the session serializes merges at wave boundaries, resolves shared-file (e.g. root import)
conflicts itself, and re-runs Phase 4 verification after each merge — not only per worker.
With `--aristotle`, each wave's lemma plans go to Aristotle as in Prove mode Phase 3.

**Phase 4 — Gate, score, packet.** Review gate per merged batch (as in Prove mode Phase 5).
When the scope is done: score a sample (or all) of the declarations with the **judge** jury +
**matcher** (eval-rubrics weights), and emit one reviewer packet for the whole run.

## Required artifacts

Whatever mode: the spec note / plan, the gate table (cycle | reviewer | verdict | reason), the
kernel evidence (`lake env lean` + `#print axioms` output), and the reviewer packet. No
`sorry`/raw `axiom` may remain in finished work — the only sanctioned gap is the project's
placeholder convention (e.g. an `unproved` macro; bootstrap one if the project lacks it), and
only where the source itself omits the proof.
