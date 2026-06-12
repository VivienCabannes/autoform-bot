# Axiom-discharge repos — proving away a classified axiom layer

Some challenge repos (e.g. `mrdouglasny/jacobian-challenge`, the community attempt at Buzzard's
Jacobian Challenge) isolate unproven classical mathematics as an **audited axiom layer**: each
`axiom` stands for a known-true theorem, tracked in a ledger, to be replaced ("discharged") by a
real proof. Working in such a repo changes the rules of engagement.

## Before touching anything

1. Read the repo's ground rules (`CLAUDE.md` / `AGENTS.md`) — they are binding. Typical hard
   rules: validate any nontrivial Lean change with `lake env lean <file>` before pushing; never
   weaken protected files (CI workflows, soundness scripts, CODEOWNERS).
2. Read the axiom's ledger entry (e.g. `AXIOM_AUDIT.md`) and its per-axiom discharge plan
   (e.g. `docs/planning/`). The plan usually names the intended technique and prior art —
   a sibling axiom discharged the same way is the best template.
3. Check the tracker issue for the axiom (often under an umbrella issue) — someone may already
   be on it.

## The spec gate is sharper here

- **Discharging** an axiom = replacing `axiom AX_foo : T` with `theorem AX_foo : T := …` with
  the statement `T` **unchanged**. Any change to `T` is a spec change, not a discharge — it
  needs project-owner discussion first.
- **Never add or strengthen an axiom** without satisfiability vetting: `lake build`,
  `#print axioms`, and CI check *typechecking only*, not truth. False axioms have shipped in
  real repos before being caught (statement-level review is the only defense). If a repo keeps
  a false-axiom post-mortem, read it.
- Watch for vacuous discharges: a proof that only works because the statement quantifies over
  an empty type or an unsatisfiable structure proves nothing — check the statement has
  inhabitants/instances.

## Landing a discharge

A discharge PR is one commit that keeps CI's soundness checks green, typically:

- the `axiom → theorem` change itself, sorry-free, helper lemmas public where reusable;
- the ledger update (count, classification move to "discharged");
- regenerated machine reports (e.g. `docs/axiom-report.txt` via the repo's script) — CI fails
  on any kernel-report diff;
- README count sync if a consistency script enforces it.

Then the PR: link and close the per-axiom tracker issue, fill required template fields (some
repos bot-enforce an "estimated human time" field), disclose AI authorship per the repo's norm
(`Co-Authored-By` / DCO), and attach the reviewer packet (→ reviewer-packet) — kernel evidence
plus the faithfulness argument for *why the statement was the vetted one all along*.

## Picking targets

Prefer, in order: (1) an axiom whose sibling was just discharged (reuse the technique), (2)
self-contained single-lemma axioms, (3) clustered structural axioms with a written plan. Avoid
research-grade axioms (the ledger usually marks them) unless the task says otherwise — there
the risk is *formulation*, not proof difficulty, and statement changes need humans.
