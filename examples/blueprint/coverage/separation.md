---
kind: coverage
status: in-progress
---

# Separation coverage

This page answers which representative results are required for the roadmap.
Its status is intentionally human-readable project policy rather than a second
machine-enforced graph.

| Status | Target | Node | Lean declaration |
| --- | --- | --- | --- |
| `REUSE` | Convex set | [Node](../nodes/definitions/convex-set.md) | `Convex` |
| `READY` | Supporting hyperplane | [Node](../nodes/lemmas/supporting-hyperplane.md) | — |
| `PLAN` | Separation theorem | [Node](../nodes/theorems/separation.md) | — |

## Completion rule

Coverage is complete when every required node is proved or explicitly reused,
the Lean declarations compile, and mathematical and API review both pass.
