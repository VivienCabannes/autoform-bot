# Worked formalization project

This is a teaching fixture for `autoform:setup`, not a repository template. It
shows the durable Autoform layer that can live inside an existing Lean project.
The example uses a fictional parity note so no target project is tempted to
inherit its mathematics.

## Source and scope

The [roadmap](roadmap.md) maps the whole note. The [coverage policy](coverage.md)
records which sections are merely mapped, which are decomposed, and which are
out of scope. Only the parity-core milestone has detailed nodes in `graph.json`.
The [source map](sources/source-map.md) records stable labels used by
`source_refs`; these labels are internal provenance and are not published.

## State boundary

Durable and committed:

- `graph.json` and `informal_content/`;
- the Lean sources under `WorkedExample/`;
- `review_status.json`;
- `kernel/` evidence; and
- roadmap, coverage, and source-map documents.

Local only:

- `task_queue.json`, `agents_status.json`, dispatcher logs, and lock files;
- provider/backend configuration and credentials; and
- generated `.autoform/site/` output.

The local dashboard may read both categories. The GitHub Pages exporter reads
only the durable publication allowlist.

## Adaptation rule

Preserve the target repository's toolchain, imports, naming, CI, and directory
layout. Replace this note, every node, and every declaration with material from
the user's approved source scope. Use `scripts/merge_node.py` for graph edits;
do not copy `graph.json` over an existing roadmap.
