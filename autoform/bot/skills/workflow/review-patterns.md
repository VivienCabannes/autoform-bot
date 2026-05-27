# Review Patterns

Understand what reviewers check and why they reject.

## Always rejected

- Axiom smuggling (pattern f): converting sorry to axiom, or splitting 1 sorry into 2+ axioms with a trivial combiner. Reviewers run `lean_verify` and check axiom lists. Only `sorryAx` is tolerated.
- Sorry count increase: any change that adds more sorrys than it removes.
- Cosmetic-only commits: docstring changes, comment additions, variable renames without actual proof work.

## Handling rejection

- Don't flip-flop between approaches in the same attempt. If approach A is rejected, refine it rather than switching to B.
- Don't acknowledge mismatches between task prompt and book content in docstrings — reviewers cite worker acknowledgment as ammunition for rejection.
- If contradictory feedback arises after 2+ rejections on the same approach, the task may be fundamentally broken. Flag for orchestrator re-scoping.
