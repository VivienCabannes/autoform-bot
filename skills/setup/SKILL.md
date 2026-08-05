---
name: setup
description: >-
  Set up, inspect, or repair repository infrastructure for an Autoform Lean
  project, including the Lean/Mathlib shell, an in-repository
  Obsidian-compatible blueprint vault, ignore rules, MkDocs, GitHub Pages, and
  verification CI. Use for new repositories, environment repair, publication
  setup, or infrastructure checks; do not choose mathematical scope or build
  the roadmap and theorem DAG.
---

# Set up an Autoform repository

State the contract before touching anything: Setup prepares a Lean repository
and an empty blueprint shell — toolchain, vault, ignore rules, MkDocs, CI, and,
on request, publication. It does not read sources, choose theorems, or write
roadmap nodes. Say so in the opening message and name Roadmap as the next step,
so the user knows which skill owns the mathematics.

Then ask how the user wants to work, because that answer gates every later
question:

- interactive — confirm each decision, including publication, as it arises;
- autonomous — take the safe defaults, perform no outward-facing action, and
  list every assumption in the closing report.

In autonomous mode ask nothing further: work locally, create nothing remote,
and leave publication to a later interactive run.

When an interactive run starts without arguments, ask one consolidated question
rather than a sequence — whether this is a new project, a repair of an existing
one, or inspection only; the Lean package name in UpperCamelCase and its target
directory; and whether to publish once the repository is ready.

Ask what the project will formalize only far enough to name that package, as in
`RudinCh3`, `ConvexBodies`, or `CabannesThesis`. Record nothing about sources,
chapters, or theorems, and do not let the answer grow into scoping: confirming
the corpus and decomposing it is Roadmap's first task, not Setup's last.

Inspect the target repository before writing and preserve existing Lean,
Markdown, workflow, and ignore files. Setup prepares the shell and stops before
mathematical planning.

Read the repo-shaped [Cabannes thesis project](assets/cabannes-thesis-project/README.md)
as a concrete setup example. Reuse its structure selectively: rename the Lean
package, check the current matching stable Lean/Mathlib release, update branch
and immutable workflow pins, and merge rather than overwrite. Its populated
thesis notes illustrate later skills; Setup does not reproduce that mathematics.

For a new or incomplete repository:

- create or repair a buildable Lean project with matching `lean-toolchain` and
  Mathlib revisions;
- create `blueprint/` with a landing page plus `roadmap/`, `coverage/`,
  `sources/`, and `nodes/`, keeping personal `.obsidian/`, `.trash/`, generated
  graphs, and site output ignored;
- configure `mkdocs.yml` so the same relative-link Markdown works locally in
  Obsidian and on the generated site;
- adapt `autoform-verify.yml` to validate the Markdown DAG, build Lean, reject
  unfinished or unsafe proofs, and audit theorem axioms on pull requests; and
- adapt `blueprint-pages.yml` to validate, render the dependency graph, build
  MkDocs, and deploy GitHub Pages from committed content.

Adding workflow files is a local repository edit. Creating a remote, pushing,
or enabling Pages are separate outward-facing actions; perform them only when
the user requests them. Pin third-party Actions and the Autoform CLI source to
immutable commits.

Validate the prepared repository with the corresponding local commands:

```bash
lake exe cache get   # skip only when the project has no Mathlib dependency
lake build
uv run --project "<AUTOFORM_PLUGIN_ROOT>" autoform check blueprint
uv run --project "<AUTOFORM_PLUGIN_ROOT>" autoform-visualize blueprint \
  --output blueprint/dependencies.html --link-extension .html
uv run --with mkdocs --with pymdown-extensions mkdocs build --strict
```

Publication is opt-in and interactive-only. Ask one concrete question naming
the exact repository — create private `<owner>/<name>` and push? — defaulting
the name to the project directory and the visibility to private, then run the
sequence rather than handing the user commands to type. Enable Pages before the
first push so the initial `blueprint-pages.yml` run deploys instead of failing:

```bash
gh repo create <owner>/<name> --private --source=. --remote=origin
gh api -X POST repos/{owner}/{repo}/pages -f build_type=workflow
git push -u origin "$(git branch --show-current)"
```

That answer approves this repository only; committing CI, enabling Pages on an
existing remote, or any later outward-facing action needs its own question.
Pages on a private repository requires a paid GitHub plan, so publishing a
blueprint of unpublished results is a decision for the user, not a default.
Treat `gh` as optional: when it is missing, unauthenticated, or the project
lives on another host, report the equivalent commands instead of running them.

Report the Lean toolchain, vault path, CI and Pages files, validation results,
and any one-time GitHub setting. State explicitly that no sources were scoped,
roadmap nodes created, or proofs started, then hand the repository to Roadmap.
