---
name: setup
description: Create, inspect, repair, or publish an in-repository Autoform Markdown blueprint for a Lean project, including roadmap and coverage notes, theorem DAG nodes, an Obsidian-compatible vault, and a GitHub Pages site.
---

Inspect the Lean root, existing documentation, and mathematical sources before writing. Establish the intended scope with the user when it cannot be recovered from the repository.

Adapt the repo-shaped [`examples/`](../../examples/README.md) consumer example; do not copy its mathematics. Keep the convention transparent and editable:

- Make `blueprint/` a portable Markdown vault with a root index, high-level `roadmap/`, project-defined `coverage/`, theorem-sized `nodes/`, and optional `sources/` and committed assets.
- Keep only `nodes/**/*.md` machine-enforced: one H1-titled node per file, optional `kind`/`status`/`lean` frontmatter, and dependency links only under `## Depends on`.
- Use YAML scalar properties, portable relative Markdown links, and ordinary MathJax delimiters so the same files work in Obsidian and a static site. Ignore personal `.obsidian/` and `.trash/` state.
- Adapt `mkdocs.yml` and `.github/workflows/blueprint-pages.yml` so pull requests validate/build and pushes to the default branch deploy GitHub Pages. Generate the exact DAG inside the vault with HTML node links before the MkDocs build.

Preserve and merge existing Markdown, workflows, and ignore rules; never overwrite them wholesale. Treat generated HTML and site output as disposable. After changes, run the bundled checker and exporter plus the example's MkDocs build, then report the vault path, generated views, Pages workflow, and any remaining one-time repository setting.
