---
name: plan-json-schema
description: Schema v4 contract for unified DAG cells, authored wiki bodies, edge evidence, and generated navigation
---

# Autoform Cell Blueprint Contract (v4)

An Autoform project has four deliberately separate layers:

1. Each theorem-sized **cell** is simultaneously a DAG node and a wiki topic.
   `graph.json` owns its stable identity, hierarchy, evidence-bearing edges,
   targets, aliases, source registration, Mathlib mapping, and Lean mapping.
2. `wiki/` supplies the authored mathematical body and supporting knowledge
   attached to those cells: informal statements, proof narratives, source
   maps, concept synthesis, audits, and modeling decisions.
3. `kernel/` and `review_status.json` hold proof and review evidence. Status is
   derived from these artifacts and the graph; it is never authored in wiki
   frontmatter.
4. `wiki/_generated/`, the local dashboard, and GitHub Pages are projections.
   They may be deleted and rebuilt without losing mathematical knowledge.

The generated cell page joins these layers into one retrieval unit. Markdown
links aid exploration, but they never create graph edges or change proof state.

The vocabulary follows WikiLean Brain where it improves retrieval without
changing Autoform's scheduling semantics:

- a tier-2 theorem node is a **cell**;
- its wiki body, sources, Lean/Mathlib declarations, reviews, and kernel
  evidence are **organs** attached to that cell;
- a tier-1 cluster is a **supercell**;
- a typed graph edge is a **synapse**, retaining provenance and evidence.

## Repository shape

```text
graph.json
wiki/
  README.md
  nodes/          # one canonical informal statement/proof page per linked node
  sources/        # adopted-source maps and exact locators
  papers/         # paper-level notes keyed by stable citation identifiers
  concepts/       # cross-node synthesis and notation
  audits/         # durable mathematical review findings
  decisions/      # accepted modeling decisions and rationale
  _generated/     # deterministic cells, supercells, aliases; never hand-edited
kernel/            # checked proof evidence
review_status.json # review evidence (local by default; publish only allowlisted fields)
.autoform/         # local queues, leases, logs, provider state, and caches
```

Operational files such as `agents_status.json`, `task_queue.json`, provider
configuration, dispatcher logs, credentials, and machine-specific paths never
belong in the wiki or public site.

## Top-level graph

```json
{
  "version": 4,
  "metadata": {
    "lean_root": "/local/path/not-for-publication",
    "sources": [],
    "targets": []
  },
  "nodes": {},
  "edges": []
}
```

`nodes` is a map keyed by stable ID. The key and each record's `id` must agree.
IDs survive title, file, and declaration renames. `metadata.targets` contains
node IDs or objects with a `node` field and identifies the mission sinks used
for prioritization and progress metrics.

## Node record

Every node uses the same record shape. Required fields should be present even
when their arrays are empty.

| Field | Type | Meaning |
| --- | --- | --- |
| `id` | string | Stable identity, equal to the map key. |
| `name` | string | Human-readable title. Defaults to the ID in presentation code. |
| `tier` | integer | `1` cluster, `2` theorem-sized mathematical node, `3` Lean-level refinement when used. |
| `parent` | string or null | The only authored hierarchy edge; it resolves exactly one tier upward. |
| `kind` | string | `definition`, `lemma`, `theorem`, `construction`, `section`, or another precise mathematical kind. |
| `description` | string | Short structural summary, not a replacement for the linked wiki page. |
| `statement_depends_on` | string array | Nodes needed to state or type this node. |
| `proof_depends_on` | string array | Additional nodes needed only by its proof or construction. |
| `depends_on` | string array | Ordered de-duplicated union of the two typed arrays. Compatibility field consumed by the current scheduler; maintained by `merge_node.py`. |
| `related` | string array | Non-blocking conceptual links. They resolve but do not affect readiness or trust. |
| `aliases` | string array | Stable lookup handles such as legacy names or domain terminology. Titles and Lean/Mathlib declarations are indexed automatically. |
| `mathlib_status` | string | Exactly `in-mathlib`, `partial`, or `missing`. |
| `mathlib_declarations` | string array | Concrete declarations supporting the Mathlib classification. |
| `mathlib_file` | string or null | Mathlib location when useful. |
| `mathlib_verified` | object or null | Verification evidence for an `in-mathlib` claim. |
| `origin` | string | `cited`, `bridged`, or `background`. |
| `source_refs` | object array | Typed links into `metadata.sources`, described below. |
| `content` | string or null | Repo-relative canonical authored page, normally `wiki/nodes/<slug>.md`. |
| `lean_file` | string or null | Repo-relative Lean file associated with the node. |
| `lean_declaration` | string or null | Compiled declaration name when one has landed. |

The dependency and `related` arrays are materialized compatibility views of
the canonical top-level `edges` table. `merge_node.py` maintains them for the
scheduler and older tools; callers must not edit both representations
independently.

Legacy schema-v2 nodes without typed arrays remain readable. Migration treats
their old `depends_on` edges as proof dependencies because that preserves the
existing scheduler semantics without claiming they are statement-level.

## Dependency semantics

Use `statement_depends_on` when the prerequisite occurs in the statement's
types, definitions, hypotheses, or conclusion. Use `proof_depends_on` only for
additional facts used to establish the statement. A node becomes schedulable
only after the union is ready, so splitting the edge types improves agent
context without weakening execution ordering.

`related` is navigation, not dependency. It may cross tiers, is excluded from
acyclicity and readiness calculations, and must never be used to make an
unsupported node look grounded.

All dependency IDs resolve within the same tier. Dependency graphs are acyclic
within each tier. `parent` resolves exactly one tier upward. Children and
coarser projections are derived from `parent` and the finest built dependency
graph; they are not stored as duplicate member arrays.

## Canonical edges

Every cell-to-cell relation is one aggregate edge in the top-level `edges`
array. Dependency direction is **dependent to prerequisite**, matching
`depends_on`:

```json
{
  "id": "edge:<deterministic-hash>",
  "source": "main-theorem",
  "target": "supporting-lemma",
  "kind": "proof-requires",
  "confidence": "high",
  "provenance": {
    "source": "BCIKS20",
    "locator": "Proof of Theorem 4.2"
  },
  "evidence": "wiki/audits/dependency-review.md"
}
```

Hard kinds are `statement-requires` and `proof-requires`; they remain directed,
within-tier, and acyclic. Soft kinds are `related`, `generalizes`, and
`special-case`; they may be cyclic and never affect readiness. `traces` may
retain multiple supporting observations behind an aggregate edge. Confidence
is `high`, `medium`, `low`, or `unknown`. Migrated edges use `unknown` and a
`legacy-node-field` provenance marker rather than inventing evidence.

## Source registry and references

`metadata.sources` is a registry of adopted source artifacts:

```json
{
  "id": "BCIKS20",
  "title": "Canonical source title",
  "citation_key": "BCIKS20",
  "url": "https://example.org/stable-record",
  "file": "sources/local-copy.pdf",
  "wiki": "wiki/papers/BCIKS20.md"
}
```

`id` is required and unique in v4. `url` should identify a stable public record;
`file` is an optional repo-relative artifact; `wiki` points to durable project
notes. A node cites it with a typed locator:

```json
{
  "source": "BCIKS20",
  "locator": "Theorem 4.2, pp. 17-18",
  "role": "statement",
  "note": "Notation translated to the project's convention"
}
```

`role` is normally `statement`, `proof`, `definition`, `motivation`, or
`counterexample`. A cited node needs at least one real reference. Bridged and
background nodes must declare their origin but must not invent citations.
Source pages may link to informal sources, arXiv records, DOI pages, Zulip
threads, Mathlib documentation, and Lean declarations. They should summarize
what each source contributes rather than copy its prose.

## Authored wiki pages

`wiki/nodes/` pages contain the canonical informal mathematics. A theorem page
normally has one H1, its statement, a proof or proof strategy when not already
in Mathlib, and links to relevant source/concept pages. The graph owns the
formal dependency lists, so prose links are explanatory and may not silently
change topology.

Keep node prose in a consistent Mathlib-aligned notation and the project's own
voice. Cite precise source locations in source maps rather than copying long
passages. Lean docstrings may use compact citation keys and link back to the
corresponding wiki page.

The other authored sections serve different retrieval needs:

- `sources/` and `papers/` answer where a claim came from;
- `concepts/` answer how recurring ideas and notation fit together;
- `audits/` preserve accepted reviewer findings worth retaining;
- `decisions/` explain why a modeling choice was made.

Do not duplicate proof status in those pages. The engine derives it from Lean,
kernel evidence, reviews, and dependency state.

## Generated wiki

Run:

```bash
python scripts/wiki_blueprint.py <project> build
python scripts/wiki_blueprint.py <project> check
```

The generated projection includes a graph revision, mission targets, tier
indexes, full cell pages under `_generated/cells/`, supercells, typed
neighborhoods with edge evidence, aliases, dependents, children, source links,
review summaries, and kernel-evidence presence. Each cell page embeds its
authored Markdown body; it does not merely link to a parallel page. It excludes
timestamps and absolute Lean paths from its revision hash. Identical durable
inputs produce identical files.

Agents can retrieve bounded local context without loading the whole graph:

```bash
python scripts/wiki_blueprint.py <project> cell "<id-or-alias>"
python scripts/wiki_blueprint.py <project> neighborhood "<id-or-alias>" --depth 1
python scripts/wiki_blueprint.py <project> search "<text>"
```

`wiki/_generated/manifest.json` records the schema, graph revision, Git commit
when available, and generated file list. Agents never edit this directory.

## Invariants

1. Node IDs and source IDs are stable and unique.
2. Every edge endpoint, `parent`, compatibility edge, target, and typed source
   reference resolves.
3. Node-local edge arrays equal the deterministic projection of canonical
   edges.
4. Dependency edges stay within a tier and form a DAG; parents sit exactly one
   tier above their children.
5. Every missing node reaches verified Mathlib grounding through dependencies
   by the end of a roadmap phase.
6. Every non-null `content` path stays inside the project and names a nonempty
   authored file. Authored node files are referenced by exactly one graph node.
7. An `in-mathlib` claim names real declarations and carries verification
   evidence before it enters the trust frontier.
8. A cited node resolves to registered sources with precise locators. Bridged
   and background nodes declare their origin explicitly.
9. Proof/review status is derived from evidence and never authored in Markdown.
10. Public exports use an explicit allowlist and contain no operational state,
    credentials, provider settings, logs, or machine paths.

## Migration

Migration is explicit and non-destructive with respect to authored content:

```bash
python scripts/wiki_blueprint.py <project> migrate
```

For schema v2 it moves linked `informal_content/*.md` pages into `wiki/nodes/`,
normalizes source IDs/references, adds typed edges, and writes schema v4. For a
Markdown-first `blueprint/roadmap/`, it imports `kind: node` pages, converts
`## Depends on` and `## Proof depends on` links into typed edges, and retains
the Markdown body as authored cell content. Schema-v3 node arrays are lifted
into evidence-bearing edges without changing scheduler order. It refuses path traversal,
symlinked wiki sections, duplicate source IDs, and authored-file overwrites.

Existing v2/v3 graphs remain readable until migration. New projects initialize
at v4. Keep compatibility fallbacks until every active project has migrated.
