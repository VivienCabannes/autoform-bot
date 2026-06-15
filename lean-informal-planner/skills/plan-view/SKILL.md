---
name: plan-view
description: >
  This skill should be used when the user asks to "view the plan",
  "show the graph", "open the visualization", "regenerate the graph",
  "refresh the plan view", or wants to see the interactive tiered
  dependency graph for a formalization plan.
version: 0.3.0
---

# Plan Visualization

Build and open the interactive tiered dependency graph for a formalization plan.
The view is a `leanblueprint` web project, generated from the plan's `graph.json`
and its `informal_content/*.md` files and laid out in the browser. It has a **Tier
dropdown** that switches granularity between the coarse tier-1 cluster map and the
fine tier-2 statement graph.

The pipeline has four steps, run in order: check the toolchain, export the
blueprint project, build the web with plasTeX, then serve and open it. Each step's
output feeds the next, so stop and report if any step fails.

## 1. Check the toolchain

The view depends on graphviz and a handful of Python packages. Verify them first
so a missing dependency surfaces as a clear message rather than a build crash:

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/check_toolchain.sh
```

The script prints a `PASS`/`FAIL` line per requirement (Python ≥ 3.10, the `dot`
binary, and each Python import) and the exact fix command for anything missing. It
exits `0` only when everything passes. If it fails, stop here and point the user at
`${CLAUDE_PLUGIN_ROOT}/SETUP.md` for the full install recipe — including the
recommended dedicated-venv path and the `LEAN_PLANNER_PYTHON` override for pinning
a specific interpreter. Do not attempt the later steps until the toolchain check
passes.

No LaTeX is required: the web build is pure-Python plasTeX, so the user does not
need a TeX distribution.

## 2. Export the blueprint project

Generate the blueprint project from the plan with the exporter:

```bash
python ${CLAUDE_PLUGIN_ROOT}/scripts/export_blueprint.py <graph.json> [--content <dir>] [--out <dir>]
```

`<graph.json>` is the plan's structure file. `--content` is the directory of
`<id>.md` prose files (default: `informal_content/` next to `graph.json`), and `--out` is
where the project is written (default: `blueprint_export/` next to `graph.json`).
The exporter reads the whole graph, emits one blueprint environment per tier-2 node
in dependency order, and generates an authoritative per-tier graphviz DOT for every
tier present (the coarse tier-1 graph being the quotient of the fine tier-2 edges).

It produces a complete, ready-to-build project under the output directory:

```
<out>/blueprint/src/web.tex        # the document, with our tier-toggle template wired in
<out>/blueprint/src/plastex.cfg    # plasTeX build config
<out>/blueprint/src/content.tex    # one environment per tier-2 node
<out>/blueprint/src/tier_dots.js   # per-tier DOT strings + cluster metadata
<out>/blueprint/src/macros/        # shared math + theorem-environment macros
```

The exporter only emits files; it never runs the build. It prints the output path,
the tiers present, and the blueprint package options line it wrote.

## 3. Build the web with plasTeX

Build from the generated `src` directory so the config's relative paths resolve:

```bash
plastex -c plastex.cfg web.tex
```

Run this from `<out>/blueprint/src`. plasTeX lays the dependency graph out in the
browser via WASM (`d3-graphviz`), so the build itself needs no LaTeX — only the
Python toolchain verified in step 1. The output is written to `<out>/blueprint/web/`,
where `index.html` is the main page and `dep_graph_document.html` is the dependency
graph.

`plastex` is a console script, so invoke it directly. The bare `plastex` above works
when the one on `PATH` belongs to the interpreter that holds the toolchain; when that
interpreter is a venv or a pinned `LEAN_PLANNER_PYTHON`, call its own console script,
which sits beside it:

```bash
"$(dirname "$LEAN_PLANNER_PYTHON")/plastex" -c plastex.cfg web.tex
```

The same entry point is also reachable through the interpreter itself, should the
console script not be on hand:

```bash
"$LEAN_PLANNER_PYTHON" -c "from plasTeX.client import main; import sys; main(sys.argv[1:])" -c plastex.cfg web.tex
```

## 4. Serve it over HTTP and open it

The dependency graph is laid out by a WASM module, which browsers only load over
HTTP — opening the HTML as a `file://` URL leaves the graph blank. Serve the built
`web/` directory over a local HTTP server and open the served URL.

Bind a fresh port and read back the one chosen, so the URL always points at this
build rather than a server left running from an earlier view. Letting the OS pick a
free port (port `0`) is the simplest way:

```bash
cd <out>/blueprint/web
python -u -m http.server 0 > /tmp/plan-view-server.log 2>&1 &
sleep 1
PORT=$(sed -n 's/.*port \([0-9][0-9]*\).*/\1/p' /tmp/plan-view-server.log | head -1)
```

The `-u` matters: a backgrounded server block-buffers its output, so without it the
port line never reaches the log in time to read.

Open the **dependency-graph page**, `dep_graph_document.html`.

Confirm the server is serving *this* build before opening — fetch the graph page and
check a node from the current plan is present:

```bash
curl -s "http://localhost:$PORT/dep_graph_document.html" | grep -q "<a node id from this plan>" \
  && open "http://localhost:$PORT/dep_graph_document.html"
```

Give the user the full `…/dep_graph_document.html` URL, and note the server keeps
running until stopped — stop a view's server once done so its port frees up.

Once it's open, point out the **Tier dropdown** at the top of the dependency-graph
page: it switches the view between the coarse tier-1 cluster map (the quick scoping
overview) and the fine tier-2 statement graph. Clicking a tier-2 node opens its
statement; clicking a coarse tier-1 cluster shows its description together with the
statements it contains (or, before it has been split in Phase 2, its planned
contents).

## When to use

- After creating or updating a formalization plan, to see the result.
- When the user asks to view the current state of the graph at either tier.
- After manual edits to `graph.json` or any `informal_content/*.md` file — rerun the export,
  build, and serve steps to refresh the view.
- To switch between the coarse and fine views of an existing plan via the Tier
  dropdown (no rebuild needed once it's open).

## What the visualization shows

- **Tiered granularity**: the Tier dropdown toggles between tier-1 clusters and
  tier-2 statements, driven by per-tier DOT strings the exporter generates.
- **Status colors** (from each node's `mathlib_status`): green filled for
  `in-mathlib`, green border for `partial`, orange border for a `missing` node not
  yet ready to state, and an auto-derived blue border for a `missing` node whose
  prerequisites are all in place.
- **Node shapes**: boxes for definitions, ellipses for other statements.
- **Dependency edges**: each `depends_on` edge drawn within its tier; coarse
  tier-1 edges are the quotient of the fine tier-2 edges.
- **Statement modal**: clicking a tier-2 node opens its paraphrased statement (and
  proof, for non-Mathlib nodes); clicking a tier-1 cluster shows its informal
  description plus the statements it contains (or, in Phase 1, its planned contents).
