# Worked setup: a thesis blueprint

The companion [blueprint vault](../assets/cabannes-thesis-project/blueprint/README.md),
[MkDocs configuration](../assets/cabannes-thesis-project/mkdocs.yml), and
[Pages workflow](../assets/cabannes-thesis-project/.github/workflows/blueprint-pages.yml)
model the start of a formalization project for Vivien Cabannes's thesis,
*From Weakly Supervised Learning to Active Labeling* ([arXiv:2209.11629](https://arxiv.org/abs/2209.11629)).

It stays intentionally small:

- one roadmap maps the thesis's six mathematical chapters;
- one coverage page distinguishes mapped, decomposed, and out-of-scope material;
- one source page records stable thesis labels; and
- five DAG nodes decompose a representative slice of the “Infimum Loss” chapter.

This is the useful asymmetry: a project can map its whole source before every
chapter is ready for theorem-level decomposition. Add nodes only after checking
the corresponding source; do not invent a complete DAG from a table of
contents.

Copy or merge the asset's `blueprint/`, `mkdocs.yml`, ignore rules, and Pages
workflow into the target Lean repository. Then replace its scope, citations,
and nodes with the target project's own material.

From the target repository root, validate the adapted result with:

```bash
autoform check blueprint
autoform-visualize blueprint \
  --output blueprint/dependencies.html \
  --link-extension .html
mkdocs build --strict
```
