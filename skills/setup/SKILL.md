---
name: setup
description: >-
  Set up, inspect, or repair repository infrastructure for an Autoform Lean
  project, including the Lean/Mathlib shell, an in-repository
  Obsidian-compatible blueprint vault, ignore rules, MkDocs, GitHub Pages, and
  verification CI, with optional Zulip community synchronization. Use for new
  repositories, environment repair, publication setup, infrastructure checks,
  or an explicitly requested Zulip project sync; do not choose mathematical
  scope or build the roadmap and theorem DAG.
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
- create `blueprint/` with a landing page plus `roadmap/`, `coverage/`, and
  `sources/`; later Roadmap work places `kind: article` pages beside their
  milestones under `roadmap/`. A roadmap folder is a book chapter; an article page
  is normally one PR-sized major result or important definition. Markdown
  properties and dependency links are the graph, so do not create a parallel
  `graph.json`. Keep personal `.obsidian/`, `.trash/`, generated graphs, and
  site output ignored;
- preserve or create the root `README.md` with a link to `blueprint/README.md`
  and a linked “Developed with AutoformBot” credit; when a deployed site
  exists, also feature its verified canonical URL prominently, never an
  inferred or pending URL;
- configure `mkdocs.yml` to build the `autoform render` output, not the vault
  itself: `docs_dir: site-src`, `md_in_html`, a `pymdownx.superfences` mermaid
  fence, a verified repository URL in `repo_url`, and the generated stylesheet
  and mermaid init. Keep the
  project-independent primary navigation focused on the Blueprint book, its
  generated Progress page, and Dependencies. Point Blueprint at the rendered
  `README.md` contents page. Use a small theme override to link the formalized
  code repository, credit AutoformBot, and retain the MkDocs footer. Remove
  MkDocs' global previous/next controls: Autoform derives those links from the
  blueprint's Markdown reading order and renders them only at the bottom of
  book pages, never on Progress or Dependencies;
- adapt `autoform-verify.yml` to validate the Markdown DAG, build Lean, reject
  unfinished or unsafe proofs, and audit theorem axioms on pull requests; and
- adapt `blueprint-pages.yml` to validate the DAG and its `lean:` declarations,
  render the blueprint, build MkDocs, and deploy GitHub Pages.

Adding workflow files is a local repository edit. Creating a remote, pushing,
or enabling Pages are separate outward-facing actions; perform them only when
the user requests them. Pin third-party Actions and the Autoform CLI source to
immutable commits.

Validate the prepared repository before reporting it ready. Build Lean first,
then run the publication sequence:

```bash
lake exe cache get   # skip only when the project has no Mathlib dependency
lake build
```

Then validate, visualize, render, and strict-build the site, keeping
`--require-declarations` so a named Lean declaration that does not exist fails
here rather than in CI. The exact invocations, including how to resolve
`<AUTOFORM_PLUGIN_ROOT>`, are in the [CLI reference](../../autoform_cli/README.md#commands);
do not restate them here.

`render` writes a derived tree; the vault stays the source of truth. Ignore
`site-src/`, `site/`, and `blueprint/dependencies.md`.

Publication is opt-in because files under `blueprint/` become public site
content, together with derived progress, graph pages, and a path-free
publication manifest. Show that boundary, confirm the exact repository and
visibility, default to private, and warn that private Pages may require a paid
GitHub plan. Rendering rejects symlinks and operational or sensitive files.
When approved, prepare the commit, remote, Pages source, and push; otherwise
leave the workflow inert.
If credentials, hosting, or repository settings block publication, report the
minimal owner action required.

Zulip synchronization is a separate opt-in outward-facing action. When the user
asks to discover community context or announce and coordinate the project, read
and follow [the shared Zulip workflow](references/zulip.md). Do not infer consent
to post from repository setup, roadmap work, or permission to search.

Report the Lean toolchain, vault path, CI and Pages files, validation results,
the publication decision, and any one-time GitHub setting the user must still
apply. State explicitly that no sources were scoped, roadmap nodes created, or
proofs started, then hand the repository to Roadmap.
