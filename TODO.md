# TODO

- Test installation instruction for Claude Code, Codex and others
- Extend the skills and commands

## installation
- Ensure installation instructions are clear
- Use `uv` to create an environment to run the MCP, try to avoid using MCP for tools that are already native to AI coding agents, or can be implemented through text file for examples
- Ensure the plugin works with Claude, Codex, and later Cursor, ...

# Skils and Commands
## Before a project
- [ ] Setup a Lean project repo
    - Tentatively use the one at https://github.com/leanprover-community/LeanProject
- [ ] Find relevant content on Zulip
    - [ ] Debug the Zulip MCP, how to make sure "zulip Python" library is installed
- [Charles] Map a curriculum of results
- [ ] Find relevant Mathlib infra to map what is missing and create a blueprint

## During a project
- [ ] Formalize incrementally in a DAG fashion
- [ ] Specialized agents for generation
- [ ] Specialized agents for reviewing
- [ ] Specialized agents for maintenance and triage
- [ ] Help humans review the process all along

## After a project
- [ ] Golf proofs
- [ ] Find connections to other formalized results
- [ ] Ensure things are scalable
- [ ] Reflect on the project and create generalizable take-aways for other formalization projects

---

### Miscellaneous Ideas (AI generated)

- **`/formalize [file.txt]`**: Translates informal mathematical proofs or pseudocode from a file into a target formal language.
- **`/spec [function_name]`**: Generates a formal specification or precondition/postcondition block for a specific piece of standard code.
- **`/tactic-suggest`**: Looks at a current open goal in Lean/Coq and uses the LLM to suggest the next tactic or proof step.
- **`/explain-error`**: Parses cryptic compiler errors from the formal verification engine and explains them in plain English.
- **`/deformalize`**: Takes an existing formal proof and translates it back into readable LaTeX or plain English.
- **`/formalize-stmt [file.tex]`**: Translates an informal mathematical statement from LaTeX/English into a mathematically equivalent Lean 4 `theorem` or `lemma` signature, leaving the proof as `sorry`. (Statement autoformalization is a major research focus, as seen in benchmarks like ProofNet).
- **`/find-premise "English description"`**: Mathlib4 is massive (~1 million lines of code), and finding the exact name of a lemma is notoriously difficult. This command would use the LLM to map a natural language concept (e.g., "the sum of two continuous functions is continuous") to the exact Mathlib4 lemma name and import path.
- **`/repair-proof`**: Takes a broken proof block and the current Lean error trace (e.g., `unsolved goals` or `tactic 'simp' failed`), and asks the LLM to repair the proof. This mirrors the automated "typecheck-and-repair" loops used in modern Lean autoformalizers like UlamAI.
- **`/mathlib-doc`**: Automatically generates standard Mathlib-compliant docstrings (`/-- ... -/`) for a given definition or theorem, explaining the formal math in plain English.
- `/formalize` (informal text -> formal spec)
- `/explain-goal` (explains the current proof state)
