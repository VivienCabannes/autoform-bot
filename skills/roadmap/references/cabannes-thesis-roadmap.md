# Worked roadmap: Cabannes thesis

The companion [blueprint vault](../../setup/assets/cabannes-thesis-project/blueprint/README.md)
models the start of a formalization project for Vivien Cabannes's thesis,
*From Weakly Supervised Learning to Active Labeling*
([arXiv:2209.11629](https://arxiv.org/abs/2209.11629)). Confirm the actual
source revision adopted by the project before relying on its paths or labels.

The example stays intentionally small:

- the roadmap maps all six mathematical chapters at coarse granularity;
- the coverage page distinguishes mapped, partial, and out-of-scope material;
- the source page records stable labels from `infimum/core.tex`; and
- five DAG nodes decompose one representative “Infimum Loss” slice.

This asymmetry is deliberate. The six chapter entries are candidate roadmap
clusters, while only one cluster has theorem-sized nodes. The example is not a
completed whole-thesis plan: comparison, consistency, learning-rate results,
and the other chapters still need source inspection and decomposition.

The detailed slice uses labels verified in `infimum/core.tex` from the public
[arXiv e-print source](https://export.arxiv.org/e-print/2209.11629):

| Node | Source label | Prerequisites |
| --- | --- | --- |
| Eligibility | `il:def:eligibility` | — |
| Non-ambiguity | `il:def:non-ambiguity` | — |
| Infimum loss | `il:thm:infimum-loss` | Eligibility |
| Non-ambiguity determinism | `il:thm:ambiguity` | Non-ambiguity |
| Supervision recovery | `il:thm:non-ambiguity` | Infimum loss; Non-ambiguity determinism |

Use the pattern—whole-source map, explicit coverage contract, approved small
slice, then dependency links—not the thesis mathematics. Validate the example
with `autoform check` and inspect its generated graph before handing ready nodes
to Orchestrate.
