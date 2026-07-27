# Proof strategies

Work incrementally. Prototype before editing. Search before proving.

## Incremental approach

- Fix compilation errors first, then triage `sorry`s by difficulty (easy → medium → hard).
- Prove the easy statements first and commit each one. Leave hard ones for later.
- For a large proof, prove the helper lemmas independently and commit them first, then assemble.

## Prototyping in the REPL

- Use `run_lean_code` (the `autoform-repl` MCP) to test proof fragments **before** editing large
  files. A large file can take 120+ seconds to rebuild — testing a fragment in isolation is far
  faster.
- Build the proof up incrementally in the REPL; only write to the file once the fragment is
  known to compile. Note the REPL environment only has `import Mathlib`, so for fragments that
  reference custom project definitions you must paste those defs into the snippet (see
  `tool-usage.md`).

## Search first

- Search Mathlib before writing a proof from scratch: `mathlib_grep` / `mathlib_find_name` (the
  mathlib MCP), or `exact?` / `apply?` / `rw?` inside a `run_lean_code` snippet.
- Many standard results already exist — finding the right lemma name is faster, and far more
  robust, than reproving a known fact. See **autoform** for naming patterns that make
  the search land.
