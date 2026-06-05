# Build & Performance

## Import Strategy

- `import Mathlib` imports the entire library (120-260s for files >50KB). Use targeted imports instead.
- Find the right module: `lean_loogle("TangentSpace")`.
- Any edit invalidates `.olean` cache for that file and all dependents. Minimize edits.

## Heartbeats

- `set_option maxHeartbeats 400000` (or higher) for computationally heavy proofs.
- Place BEFORE the declaration, not inside the proof.

## Large File Strategies

- Files >50KB will likely timeout on `lake build`. Use `lean_verify` for individual declarations.
- Prototype in standalone files via `run_lean_code` before editing large files.
- Avoid adding new imports to large files — each import expands the dependency graph.

## Elaboration Speed

- Generalize `EuclideanSpace ℝ (Fin n)` to generic `NormedAddCommGroup E` when possible — avoids product type elaboration slowness.
- Explicit scalar field annotations (`(𝕜 := ℝ)`) speed up elaboration by avoiding instance search.
- Use `smul_smul` instead of `mul_smul` to avoid `MulAction` instance search.
