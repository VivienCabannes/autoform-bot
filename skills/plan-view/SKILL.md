---
name: plan-view
description: >
  This skill should be used when the user asks to "view the plan",
  "show the graph", "open the visualization", "regenerate the graph",
  "refresh the plan view", or wants to see the interactive tiered
  dependency graph for a formalization plan.
version: 0.4.0
---

# Plan Visualization

Build and open the interactive tiered dependency graph for a formalization plan.
The view is a `leanblueprint` web project, generated from the plan's `graph.json`
and its `informal_content/*.md` files and laid out in the browser. It has a **Tier
dropdown** that switches granularity between the coarse tier-1 cluster map and the
fine tier-2 statement graph.

The project follows the standard leanblueprint layout and ships with a `Makefile`
for setup and building. The pipeline has four steps, run in order: check the
toolchain, export the blueprint project, build the web, then serve and open it.

## 1. Check the toolchain

The view depends on graphviz and several Python packages. Verify them first:

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/check_toolchain.sh
```

The script prints a `PASS`/`FAIL` line per requirement (Python >= 3.10, the `dot`
binary, and each Python import) and the exact fix command for anything missing. It
exits `0` only when everything passes. If it fails, stop here and run the Makefile's
`make setup-venv` target (step 3) — that is the supported fix — or see
`${CLAUDE_PLUGIN_ROOT}/SETUP.md` for the manual path. Do not attempt later steps
until the toolchain is set up.

Note: a pygraphviz FAIL of the form *"installed, but its graphviz runtime libs are
not on the loader path"* is **expected on platform Pythons** and is **not** fixed by
`pip install`. `make setup-venv` curates the graphviz libs into `.lean-deps/gvlibs/`
and `make web`/`make serve` load them via `LD_LIBRARY_PATH`, so just proceed to
step 3 — do not hand-install pygraphviz.

## 2. Export the blueprint project

Generate the blueprint project from the plan with the exporter:

```bash
python ${CLAUDE_PLUGIN_ROOT}/scripts/export_blueprint.py <graph.json> [--content <dir>] [--out <dir>]
```

`<graph.json>` is the plan's structure file. `--content` is the directory of
`<id>.md` prose files (default: `informal_content/` next to `graph.json`), and
`--out` is where the project is written (default: `blueprint_export/` next to
`graph.json`).

The exporter produces a complete, ready-to-build project:

```
<out>/Makefile                        # build orchestration (make web, make serve, etc.)
<out>/blueprint/src/web.tex           # web entry point (with tier-toggle template)
<out>/blueprint/src/print.tex         # PDF entry point (for xelatex builds)
<out>/blueprint/src/plastex.cfg       # plasTeX config (depgraph + showmore + leanblueprint)
<out>/blueprint/src/content.tex       # one environment per tier-2 node
<out>/blueprint/src/tier_dots.js      # per-tier DOT strings + cluster metadata
<out>/blueprint/src/blueprint.sty     # stub package
<out>/blueprint/src/extra_styles.css  # theorem border styling
<out>/blueprint/src/macros/           # shared math + theorem-environment macros
```

## 3. Set up the toolchain (required first time)

On the first build (or whenever step 1 reports failures), run:

```bash
cd <out>
make setup-venv
```

This one target does everything needed for a working build:
- curates the graphviz **runtime** libraries (with their soname symlinks) into
  `<out>/.lean-deps/gvlibs/`, so `pygraphviz` loads on platform Pythons without
  dragging in the system `libc` (`make web`/`make serve` add this dir to
  `LD_LIBRARY_PATH`);
- creates a Python venv at `<out>/.venv/` and installs a **pinned** toolchain
  (`leanblueprint`, `plastexdepgraph`, `plastexshowmore`, `plasTeX`, `pygraphviz`,
  `fastmcp`);
- applies a small hashability fix to `plastexdepgraph` (plasTeX 3.1's theorem
  classes are unhashable and 3.1 is the only version `plastexdepgraph` accepts, so
  the dep-graph build would otherwise crash with `unhashable type: 'definition'`).

Always build and serve via the Makefile targets below (`make web` / `make serve`) —
they export the `PATH` (venv) and `LD_LIBRARY_PATH` (gvlibs) the build needs. Do not
call `plastex` directly.

## 4. Build the web with the Makefile

From the output directory:

```bash
cd <out>
make web
```

This invokes plasTeX (with the venv on `PATH` and the curated graphviz libs on
`LD_LIBRARY_PATH`) to build the HTML blueprint. Output goes to `<out>/blueprint/web/`,
where `dep_graph_document.html` is the dependency graph and `index.html` is the table
of contents.

No LaTeX is required — the web build is pure-Python plasTeX.

## 5. Serve and open

```bash
cd <out>
make serve
```

This kills any existing server on port 8005 and starts a new one serving the
built blueprint. The dependency graph uses WASM (d3-graphviz), which requires
HTTP — opening the HTML via `file://` leaves the graph blank.

Open the **dependency-graph page** at `http://localhost:8005/dep_graph_document.html`.

Point out the **Tier dropdown** at the top: it switches between the coarse tier-1
cluster map and the fine tier-2 statement graph. Clicking a tier-2 node opens its
statement; clicking a tier-1 cluster shows its description and member list.

## When to use

- After creating or updating a formalization plan, to see the result.
- When the user asks to view the current state of the graph at either tier.
- After manual edits to `graph.json` or `informal_content/*.md` — rerun the export
  and `make web` to refresh the view.

## What the visualization shows

- **Tiered granularity**: the Tier dropdown toggles between tier-1 clusters and
  tier-2 statements.
- **Status colors** (from `mathlib_status`): green filled for `in-mathlib`, green
  border for `partial`, orange border for blocked `missing` nodes, auto-derived
  blue for ready `missing` nodes.
- **Node shapes**: boxes for definitions, ellipses for other statements.
- **Dependency edges**: within-tier edges; coarse tier-1 edges are the quotient of
  tier-2 edges.
- **Statement modals**: clicking a tier-2 node opens its paraphrased statement;
  clicking a tier-1 cluster shows its description and member list.
- **Showmore**: expand/collapse toggle for proofs on per-chapter pages.
- **Theorem styling**: vertical border accents on theorem/lemma/proof blocks.
