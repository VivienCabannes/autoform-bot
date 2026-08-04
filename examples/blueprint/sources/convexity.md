---
kind: source
status: adopted
---

# Convexity notes

These short notes stand in for the mathematical material being formalized. A
real project may keep authored notes here or link to stable external sources.

## Convex sets

A subset $C$ of a real vector space is convex when

\[
  x,y \in C,\quad 0 \leq t \leq 1
  \quad\Longrightarrow\quad
  t x + (1-t)y \in C.
\]

## Supporting hyperplanes

A supporting hyperplane touches a convex set while leaving the set in one of
the two associated closed half-spaces.

## Separation

Under suitable hypotheses, two disjoint convex sets admit a continuous linear
functional that separates them.
