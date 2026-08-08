# Worked orchestration: the Infimum Loss slice

In the [Cabannes thesis asset](../../setup/assets/cabannes-thesis-project/blueprint/README.md),
`eligibility` and `non-ambiguity` are dependency-free and may be assigned in
parallel. `infimum-loss` must wait for `eligibility`, while
`non-ambiguity-determinism` waits for `non-ambiguity`.
The Full Supervision support chapter can proceed alongside them, and
`supervision-recovery` must wait for both source branches and its supporting
definition and lemma.

For one ready node:

1. Open its cited thesis label and recover the exact assumptions and conclusion.
2. Search the target Lean project and Mathlib before choosing an API.
3. Develop and compile the declaration with the Lean tools.
4. Ask an independent reviewer to compare it with the cited source.
5. Only then set `statement: formalized` and `proof: formalized`, and add the
   actual compiled declaration under `lean:`; never guess that name from the
   Markdown title. `autoform-blueprint check --lean-root` rejects a name that is not in
   the sources.

Recheck the DAG after the edit. A newly unblocked node is the next work item;
source order alone is not a scheduling rule.
