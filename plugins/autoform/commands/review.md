---
description: Review a Lean diff for faithfulness and Mathlib style (read-only).
argument-hint: "[<ref>|--staged] [--book-dir DIR]"
allowed-tools: Read, Bash, Grep, Glob, Task
---

# /autoform:review — correctness + quality review

Review a formalization change against the source textbook, using the same two lenses as the
formalize gate. Arguments: `$ARGUMENTS`. (Session-only — there is no `--backend python`; the
Python engine reviews inline during a run.)

Resolve and **echo**: what to review (a git ref / `--staged` / working tree) and the book dir.
Load **formalization-workflow** and **eval-rubrics**.

## Steps

1. Collect the diff (`git diff <ref>` / `git diff --staged`) and the list of changed `.lean`
   files.
2. Dispatch in parallel:
   - **code-reviewer** — does it compile, is it faithful to the book (no extra hypotheses unless
     provably redundant), is it honest (grep the whole project for `sorry`/`axiom`; verify
     `#print axioms`)?
   - **quality-inspector** — Mathlib idiom / naming / structure only.
3. Merge both verdicts into one report: overall `APPROVED` only if **both** approve; otherwise
   `REJECTED` with a consolidated, de-duplicated issues + fixes list (file:line each).

## Required artifact

The two-column verdict (correctness / quality) plus the consolidated issue list. This command is
read-only — it proposes fixes but does not apply them (use `/autoform:formalize` to act on them).
