# Proof strategies

Work incrementally. Prototype before editing. Search before proving.

## Incremental approach

- Fix compilation errors first, then triage `sorry`s by difficulty (easy → medium → hard).
- Prove the easy statements first and commit each one. Leave hard ones for later.
- For a large proof, prove the helper lemmas independently and commit them first, then assemble.

## Prototyping with Lean LSP

- Use `lean_goal` and `lean_multi_attempt` from `lean-lsp-mcp` to inspect the
  current proof state and test tactics before editing large files.
- Use `lean_run_code` for self-contained fragments. For project definitions,
  query the actual file with `lean_diagnostic_messages` and `lean_goal`.
- Write a tactic only after the LSP attempt succeeds, then confirm the actual
  file still elaborates.

## Search first

- Search Mathlib before writing a proof from scratch: use `loogle` for type
  shapes and names, `lean-explore search` for semantic queries, the local
  `scripts/mathlib_search.py` CLI for source verification, and `exact?` /
  `apply?` / `rw?` inside a Lean file or `lean_run_code` snippet.
- Many standard results already exist — finding the right lemma name is faster, and far more
  robust, than reproving a known fact. See **autoform** for naming patterns that make
  the search land.
