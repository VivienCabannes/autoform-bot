# Proof Strategies

Work incrementally. Prototype before editing. Search before proving.

## Incremental approach

- Fix compilation errors first, then triage sorrys by difficulty (easy → medium → hard).
- Prove easy theorems first and commit each one. Leave hard ones for later.
- For large proofs, prove helper lemmas independently and commit them first.

## Prototyping

- Use `run_lean_code` to test proof fragments before editing large files. Large files take 120+ seconds to build — testing in isolation saves time.
- Build up the proof incrementally in the REPL, only write to file when the proof is known to work.

## Search first

- Search Mathlib using `lean_loogle`, `mathlib_find_name`, `mathlib_grep` before writing proofs from scratch.
- Many standard results already exist — finding the right lemma name is faster than reproving.
