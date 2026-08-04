# Worked setup: a durable Autoform repository

The companion
[`worked-formalization-project`](../assets/worked-formalization-project/README.md)
shows the Autoform layer of a small Lean repository using the current durable
contracts: `graph.json`, per-node `informal_content/`, source anchors, review
state, kernel evidence, and local-only operational files.

It models a useful asymmetry:

- the roadmap maps the whole source;
- coverage distinguishes mapped, decomposed, deferred, and out-of-scope areas;
- the detailed DAG covers only one approved milestone; and
- every detailed node points back to a stable source label.

This prevents a table of contents from being inflated into a fictional theorem
graph. Add fine nodes only after inspecting the corresponding source. The
source anchor is authoritative; graph descriptions and prose are reviewable
working artifacts.

## Adapt, do not install

This reference is not a project generator. Setup remains a Markdown skill that
uses the repository's existing Lean toolchain, naming, imports, CI, and source
layout. Roadmap owns source inspection and graph construction. Do not copy the
example's mathematics or overwrite existing files. Instead:

1. use Setup to inspect the target Lean repository and initialize or resume its
   durable state without reading the source corpus;
2. use Roadmap to inspect the sources and agree on the first milestone;
3. adapt the roadmap, coverage policy, source map, and node structure;
4. preserve operational state locally and durable evidence in Git; and
5. use Autoform's existing exporter and pinned workflow generator when the user
   explicitly approves publication.

The asset omits a second `lakefile.toml`, toolchain pin, and copied Pages
workflow on purpose. Setup creates or preserves the real Lean project, while
`templates/github/autoform-pages.yml` remains the single deployment template.

## Validation

After adapting the pattern, validate with the repository's ordinary Lean build,
the local dashboard, and the static exporter. A complete Setup and Roadmap
report names:

- the Lean repository and roadmap directory;
- the adopted sources and exact milestone;
- mapped versus decomposed coverage;
- tier-1 and tier-2 node counts;
- missing content, review, or kernel evidence;
- local dashboard status; and
- publication readiness without enabling publication automatically.
