---
name: autoform-extract
description: >
  Extract formalizable mathematical statements from LaTeX or Markdown source material.
  Produces structured YAML targets for the formalization pipeline.
  Use when preparing source material for formalization.
  Triggers on: /autoform-extract, "extract statements", "find theorems in".
---

# Statement Extraction

Extract definitions, theorems, lemmas, and corollaries from LaTeX or Markdown source material into structured targets for Lean 4 formalization.

## Process

1. **Read the source** — scan the document for mathematical content.
2. **Identify statements** — locate definitions, theorems, propositions, lemmas, corollaries, and examples.
3. **Classify each** — determine the type (definition, theorem, lemma, etc.) and dependencies.
4. **Extract structure** — for each statement, capture:
   - **ID** — a short kebab-case identifier (e.g., `thm-2-3-bezout`)
   - **Type** — `definition`, `theorem`, `proposition`, `lemma`, `corollary`, `example`
   - **Title** — human-readable name (e.g., "Bezout's theorem")
   - **Source reference** — chapter, section, page, theorem number
   - **LaTeX statement** — the original mathematical statement
   - **Dependencies** — IDs of definitions/theorems this statement depends on
   - **Has proof** — whether the source provides a proof (determines if `unproved` is acceptable)
5. **Output YAML** — produce a structured `targets.yaml` file.

## Output Format

```yaml
targets:
  - id: def-1-1-convex-set
    type: definition
    title: Convex set
    source: "Definition 1.1, p.3"
    latex: |
      A set $C \subseteq \mathbb{R}^n$ is \emph{convex} if for all
      $x, y \in C$ and $\lambda \in [0,1]$, we have
      $\lambda x + (1-\lambda) y \in C$.
    dependencies: []
    has_proof: false

  - id: thm-1-2-convex-combination
    type: theorem
    title: Convex combination characterization
    source: "Theorem 1.2, p.5"
    latex: |
      A set $C$ is convex if and only if it contains all convex
      combinations of its elements.
    dependencies: [def-1-1-convex-set]
    has_proof: true
```

## Guidelines

- Preserve the exact mathematical notation from the source.
- Include ALL statements, not just named theorems — unnumbered lemmas, remarks used later, and key examples matter.
- Order dependencies correctly — a statement should only depend on statements that appear before it.
- Mark `has_proof: false` only when the source genuinely does not provide a proof. A sketch or outline counts as a proof.
- Use consistent ID prefixes: `def-`, `thm-`, `prop-`, `lem-`, `cor-`, `ex-`.
