# Example formalization repository

Treat this directory as the root of a small consumer repository. It shows the
recommended convention without making the convention a rigid Autoform schema:

- `blueprint/` is committed beside the Lean source and opens directly as an
  Obsidian vault.
- `blueprint/roadmap/` contains high-level direction and progress properties.
- `blueprint/coverage/` states which families or representative results count
  toward completion.
- `blueprint/nodes/` is the only machine-enforced structure: one theorem-sized
  Markdown node per file, with dependencies under `## Depends on`.
- `blueprint/sources/` contains or links the mathematical source material.
- `mkdocs.yml` renders all Markdown, while the workflow generates the exact DAG
  and deploys the result to GitHub Pages.

Copy and adapt these files to a Lean repository rather than treating their
mathematical taxonomy or prose as mandatory.

After committing the workflow, select **Settings → Pages → Source: GitHub
Actions** once if Pages is not already enabled for the repository. Subsequent
pushes to `main` validate, build, and deploy the site automatically; pull
requests validate and build without deploying.

## Local preview

From this example directory, with Autoform available on `PATH`:

```bash
autoform check blueprint
autoform-visualize blueprint \
  --output blueprint/dependencies.html \
  --link-extension .html
uvx --from mkdocs --with pymdown-extensions mkdocs serve
```

Open `blueprint/` as a vault to use Obsidian's backlinks and graph view. The
vault deliberately ignores `.obsidian/`, so personal workspace settings remain
local.
