---
kind: source
status: adopted
---

# Thesis source map

Vivien Cabannes, *From Weakly Supervised Learning to Active Labeling*, PhD
thesis, 2022. Stable public record: [arXiv:2209.11629](https://arxiv.org/abs/2209.11629).

The initial DAG is anchored in the “Infimum Loss” chapter. When working from
the thesis source tree, verify these labels in `infimum/core.tex`:

| Blueprint target | Source label |
| --- | --- |
| Eligibility | `il:def:eligibility` |
| Non-ambiguity | `il:def:non-ambiguity` |
| Non-ambiguity determinism | `il:thm:ambiguity` |
| Infimum loss | `il:thm:infimum-loss` |
| Supervision recovery | `il:thm:non-ambiguity` |

Labels locate the authoritative statements; the node summaries are planning
notes and must not replace source inspection.
