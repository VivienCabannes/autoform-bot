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

Setup prepares the Lean toolchain, an empty blueprint vault, ignore rules,
MkDocs, CI, and optionally publication. It does not scope sources, choose
theorems, write roadmap nodes, or prove results; Roadmap owns that work.

Inspect before writing and preserve existing Lean, Markdown, workflow, and
ignore files. Infer safe local defaults from the request and repository. If a
material choice is missing, ask once for the run type (new, repair, or inspect),
UpperCamelCase package name, target directory, and whether publication is
wanted. Without explicit publication approval, make no remote changes. Setup
prepares the shell and stops before
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

Validate the prepared repository with the corresponding local commands.
`<AUTOFORM_PLUGIN_ROOT>` is the AutoformBot checkout this skill was loaded
from; substitute its absolute path:

```bash
lake exe cache get   # skip only when the project has no Mathlib dependency
lake build
uv run --project "<AUTOFORM_PLUGIN_ROOT>" autoform check blueprint
uv run --project "<AUTOFORM_PLUGIN_ROOT>" autoform-visualize blueprint \
  --output blueprint/dependencies.html --link-extension .html
uv run --with mkdocs --with pymdown-extensions mkdocs build --strict
```

Publication is opt-in because files under `blueprint/` become public site
content. Confirm the exact repository and visibility, default to private, and
warn that private Pages may require a paid GitHub plan. When approved, prepare
the commit, remote, Pages source, and push; otherwise leave the workflow inert.
If credentials, hosting, or repository settings block publication, report the
minimal owner action required.

Report the Lean toolchain, vault path, CI and Pages files, validation results,
the publication decision, and any one-time GitHub setting the user must still
apply. State explicitly that no sources were scoped, roadmap nodes created, or
proofs started, then hand the repository to Roadmap.
