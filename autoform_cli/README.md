# Blueprint format and CLI

The Autoform CLI validates and visualizes the fine-grained dependency graph in
`blueprint/nodes/`. Roadmap and coverage organization remain project policy;
the CLI deliberately enforces only the node format and graph structure.

## Nodes

Every Markdown file below `blueprint/nodes/` is one node. Its relative path
without `.md` is its stable ID. The H1 is its human title; optional frontmatter
records lightweight metadata:

```markdown
---
kind: theorem
status: ready
lean: MyProject.separatingHyperplane
---

# Separating hyperplane theorem

State the intended result and proof sketch here.

## Depends on

- [Convex set](../definitions/convex.md)

## Sources

- [Chapter 2](../../sources/convexity.md#separation)
```

Only links under `## Depends on` are graph edges. Other links remain ordinary
navigation or citations. Dependencies are resolved relative to the current
node and must point to another Markdown file inside `blueprint/nodes/`.

The conventional statuses are `planned`, `ready`, `blocked`, and `proved`. A
node is ready when all linked prerequisites are proved; a proved node should
name its compiled declaration in `lean`.

## Validation

`autoform check` rejects cycles, missing targets, escaping paths,
self-dependencies, missing H1 titles, and unsupported frontmatter. It validates
graph structure but leaves mathematical correctness and Lean declarations to
the agent and the Lean kernel.

The Markdown files are the source of truth. HTML graphs and sites are derived
views that may be regenerated at any time.
