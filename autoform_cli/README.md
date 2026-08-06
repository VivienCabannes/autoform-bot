# Blueprint format and CLI

The Autoform CLI validates, visualizes, and publishes the fine-grained
dependency graph embedded in `blueprint/roadmap/`. Roadmap and coverage
organization remain project policy; the CLI deliberately enforces only marked
node pages and graph structure.

## Nodes

A Markdown file below `blueprint/roadmap/` is a node when its frontmatter sets
`kind: node`. Its path relative to `roadmap/`, without `.md`, is its stable ID.
Other roadmap pages are ignored by the graph loader. The H1 is the node's human
title; frontmatter records what has been *checked*:

```markdown
---
kind: node
declaration: theorem
statement: formalized
proof: formalized
lean: MyProject.separatingHyperplane
---

# Separating hyperplane theorem

State the intended result and proof sketch here.

## Depends on

- [Convex set](convex.md)

## Proof depends on

- [Supporting hyperplane](supporting-hyperplane.md)

## Sources

- [Chapter 2](../../sources/convexity.md#separation)
```

`## Depends on` lists what the node needs in order to be *stated*;
`## Proof depends on` lists what only its *proof* needs. Both are graph edges.
Links anywhere else are ordinary navigation or citations. Dependencies resolve
relative to the current node and must point at another `kind: node` file inside
the blueprint.

`kind` describes the role of a Markdown page, alongside values such as
`roadmap`, `coverage`, and `source`. The optional `declaration` field describes
the intended Lean artifact, for example `def`, `theorem`, `lemma`, `structure`,
or `instance`. Autoform records this hint but does not constrain the set of Lean
declaration commands. Declarations that introduce data rather than a
proposition carry no separate proof obligation.

## Assertions and derived status

A node asserts only facts a human or agent verified:

| Key | Meaning |
| --- | --- |
| `statement: formalized` | The Lean statement exists and compiles. |
| `proof: formalized` | The Lean proof is complete. |
| `mathlib: true` | The result is upstreamed into Mathlib. |
| `not_ready: true` | Needs more blueprint work before it can be attempted. |
| `lean: Ns.decl` | Declaration name(s) that discharge the node. |
| `discussion: 42` | Issue number or URL where the node is being discussed. |

Everything a reader thinks of as progress is *derived* from the DAG on every
run, so it cannot go stale:

| Derived state | Holds when |
| --- | --- |
| `can_state` | Every statement prerequisite is stated. |
| `can_prove` | Stated, and every proof prerequisite is proved. |
| `proved` | The proof compiles. |
| `fully_proved` | Proved, and every prerequisite is fully proved, recursively. |
| `defined` | A definition is written but rests on unfinished work. |

`proved` and `fully_proved` differ on purpose: a theorem whose own proof
compiles but which rests on an unproved lemma is green, not dark green. The
palette and state names follow
[leanblueprint](https://pypi.org/project/leanblueprint/), so the published
graph reads the same way as the Lean community's LaTeX blueprints.

The flat `status:` field is deprecated. It still loads with a warning:
`proved` becomes `statement`+`proof`, `blocked` becomes `not_ready`, and
`ready`/`planned` are dropped because the graph derives them.

## Commands

Validate structure, and optionally check that every `lean:` name really exists
in the project's Lean sources:

```bash
autoform check blueprint --lean-root .
```

Write the Mermaid dependency graph into the vault, where Obsidian renders it:

```bash
autoform-visualize blueprint
```

Build the publishable site source — a book overview, aggregate progress,
statement boxes with collapsed dependency details, multi-scale dependency
maps, and direct links to Lean declarations at the current commit:

```bash
autoform render blueprint --output site-src --lean-root . --require-declarations
```

`render` never writes into the vault. It derives `progress.md`, injects a
compact progress summary into the blueprint introduction, and shows a source
icon when a `lean:` declaration resolves to a repository permalink. Its
`dependencies.md` entry point collapses nodes by textbook chapter, with links
to theorem-level chapter maps, one-hop local contexts, and the complete DAG.
Every graph node returns to the numbered statement, and every statement links
to its local context. Point `mkdocs.yml` at `docs_dir: site-src` and enable
`md_in_html` plus a `pymdownx.superfences` mermaid fence; see the [repository
example](../skills/setup/assets/cabannes-thesis-project/mkdocs.yml).

## Validation

`autoform check` rejects cycles, missing targets, escaping paths,
self-dependencies, missing H1 titles, unsupported frontmatter keys, and
assertion values it does not recognize. With `--lean-root` it also fails on a
`lean:` name absent from the sources, the job `leanblueprint checkdecls` does
for LaTeX blueprints. It validates structure and leaves mathematical
correctness to the agent and the Lean kernel.

The Markdown files are the source of truth. Graphs and sites are derived views
that may be regenerated at any time.

## Publication contract

`autoform render` publishes three views of the same committed Markdown: the
book, derived progress, and dependency maps at project, chapter, local, and
full-graph scales. It never reads a `graph.json` or an operational queue.
Hidden files are omitted, while symlinks, credentials, logs, provider state,
and agent/task state inside the blueprint cause the render to fail rather than
silently leak them. Source and output directories must be disjoint.

Every render writes `publication.json` with the source-content hash, Git ref,
node and dependency counts, and available views. It contains no timestamp or
absolute path, so identical inputs produce identical output files.
