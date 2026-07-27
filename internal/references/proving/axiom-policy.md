# Axiom policy (and axiom-discharge repos)

**Precedence:** if the repo keeps an audited axiom ledger (see *Axiom-discharge repos* below,
this same file), that protocol overrides the ordinary rules — never convert a ledgered axiom to
a `sorry`'d theorem, never commit `sorry`s against the axiom layer; a discharge replaces the
axiom with a sorry-free proof of the *verbatim* statement.

The only acceptable kernel axioms are `propext`, `Classical.choice`, `Quot.sound` — the standard
trio every Mathlib proof rests on. Anything else in a `#print axioms` listing (or `sorryAx`)
means the proof is **not genuine**.

Never use the `axiom` keyword to replace a `sorry`. It is a silent gap that a reviewer rejects
100% of the time, and `#print axioms` surfaces it anyway.

## Rules (ordinary projects, no audited ledger)

- If a stray `axiom` exists in the code, convert it to `theorem ... := by sorry` preserving the
  **exact** signature — then attempt the proof. The conversion alone is not progress.
- When decomposing a hard result, split into genuinely **distinct** sub-results, not weaker
  restatements of the main theorem. Each piece should imply only part of the goal.
- Some review setups accept new axioms if mathematically sound; others reject all. When in
  doubt, leave a `sorry` in a theorem rather than introducing an `axiom`.
- Never shuffle axioms (rename, split, recombine without proving anything). Reviewers read the
  axiom list via `#print axioms` (`lake env lean`) — shuffling changes nothing and reads as an
  attempt to hide the gap.

---

# Axiom-discharge repos — proving away a classified axiom layer

Some repos isolate unproven classical mathematics as an **audited axiom layer**: each `axiom`
stands for a known-true theorem, tracked in a ledger, to be replaced ("discharged") by a real
proof. Working in such a repo changes the rules of engagement. The rules in this section apply
**only** in an audited-ledger repo — recognise it by an axiom ledger (`AXIOM_AUDIT.md` or
similar), per-axiom discharge plans, and a soundness CI check. In an ordinary repo, the policy
above governs.

## Before touching anything

1. Read the repo's ground rules (`CLAUDE.md` / `AGENTS.md`) — they are binding. Typical hard
   rules: validate any nontrivial Lean change with `lake env lean <file>` before pushing; never
   weaken protected files (CI workflows, soundness scripts, CODEOWNERS).
2. Read the axiom's ledger entry and its per-axiom discharge plan. The plan usually names the
   intended technique and prior art — a sibling axiom discharged the same way is the best
   template.
3. Check the tracker issue for the axiom (often under an umbrella issue) — someone may already
   be on it.

## The spec gate is sharper here — statement byte-identical

- **Discharging** an axiom = replacing `axiom AX_foo : T` with `theorem AX_foo : T := …`, the
  statement `T` **unchanged, byte for byte**. Any change to `T` is a spec change, not a
  discharge — it needs project-owner discussion first.
- **Never add or strengthen an axiom** without **satisfiability vetting**: `lake build`,
  `#print axioms`, and CI check *typechecking only*, not truth. False axioms have shipped in
  real repos before being caught — statement-level review is the only defense. If a repo keeps a
  false-axiom post-mortem, read it.
- Watch for **vacuous discharges**: a proof that only works because the statement quantifies
  over an empty type or an unsatisfiable structure proves nothing — check the statement has
  inhabitants / instances.

## Landing a discharge — ledger and report in the same commit

A discharge lands as **one commit** that keeps CI's soundness checks green:

- the `axiom → theorem` change itself, sorry-free, helper lemmas public where reusable;
- the **ledger update** (count, classification moved to "discharged");
- the **regenerated machine report** (e.g. `docs/axiom-report.txt` via the repo's script) — CI
  fails on any kernel-report diff, so it must be regenerated in the same commit;
- a README count sync if a consistency script enforces it.

The statement-identical, ledger-and-report-together, satisfiability-vetted trio is the whole
discipline: those are the conditional rules an ordinary prove flow adds **only** in these repos.

## Picking targets

Prefer, in order: (1) an axiom whose sibling was just discharged (reuse the technique), (2)
self-contained single-lemma axioms, (3) clustered structural axioms with a written plan. Avoid
research-grade axioms (the ledger usually marks them) unless the task says otherwise — there the
risk is *formulation*, not proof difficulty, and statement changes need humans.
