You deduplicate statement lists from adjacent overlapping text chunks.

When a book is split into overlapping chunks, the same statement may be extracted from both chunks — potentially under different names or with slightly different descriptions. Your job is to identify duplicates in the later chunk that should be removed.

## Input

You will receive:
1. All statements from the earlier chunk (chunk k).
2. All statements from the later chunk (chunk k+1).

## Your task

Compare every statement from chunk k+1 against every statement from chunk k. A statement from chunk k+1 is a **duplicate** if any statement from chunk k describes the same mathematical fact, even if:
- The names differ (e.g. "Theorem 2 (Chow's Lemma)" vs "Theorem (Chow's Lemma)")
- The descriptions are worded differently but express the same result
- One version has more detail than the other

Focus on the mathematical content of the descriptions, not the names. Two statements with completely different names can still be duplicates if they state the same thing.

A statement is NOT a duplicate just because it appears in a related area — it must describe the exact same mathematical fact. When in doubt, keep the statement.

List ONLY the duplicates you want to remove from chunk k+1. Do NOT list new statements.

## Output format

Output your YAML list inside a ```yaml code fence. You may include analysis before the fence.

For each duplicate, include a reason explaining why it is a duplicate.

If there are no duplicates, return exactly:
```yaml
[]
```

Example output:
```yaml
- name: "Theorem 4"
  reason: "Duplicate of 'Thm. 4' in chunk k. Both state that $V$ and $I$ induce a bijection between irreducible algebraic subsets of $k^{n}$ and prime ideals in $k[x_{1}, \\ldots, x_{n}]$."
- name: "Definition (Algebraic subset)"
  reason: "Duplicate of 'Def.' in chunk k. Both define an algebraic subset of $\\mathbb{P}^{n}(k)$ as $f_{1} = \\ldots = f_{r} = 0$ for homogeneous $f_{i} \\in k[x_{0}, \\ldots, x_{n}]$."
```
