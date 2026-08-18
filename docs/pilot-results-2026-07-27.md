# Pilot results — 2026-07-27

Candidate: local `codex/full-parity` working tree based on `59fb795`. Repeat the
release gate after the hardening changes have a final commit SHA.

## Automated and local gates

| Gate | Result |
|---|---|
| Pytest, Python 3.10 | 492 passed |
| Pytest, Python 3.11 | 492 passed |
| Pytest, Python 3.12 | 492 passed |
| Pytest, Python 3.13 | 492 passed |
| Pytest, Python 3.14 | 492 passed |
| Bundled workspace demo | Passed on every tested Python series |
| Ruff (`scripts`, `servers`, `tests`, `skills`) | Passed |
| Plugin linter | 67 checks passed |
| Whitespace validation | Passed |

The matrix uncovered and fixed three compatibility defects: the workspace
script's filename shadowed Python's standard-library `inspect` module on 3.14,
`datetime.UTC` was unavailable on 3.10, and the 3.10 TOML fallback was not a
declared runtime dependency.

## Codex host preflight

- Codex CLI: `0.146.0-alpha.3.1`
- Authentication: existing ChatGPT login
- Generated project roles: 11 installed
- Idempotency check: 11 `ok`, `changed=0`
- Test data: synthetic prompt and generated role TOML only; no textbook or
  external Lean project content

## Headless prover and jury: passed

The preserved disposable Lean pilot contains auditable queue, review, and usage
artifacts:

- A Codex worker proved `autoformPilot (n : Nat) : n + 0 = n`.
- A forced one-second timeout killed the worker, raised one escalation, and was
  resolved before retry.
- The first real verification pass exposed a Lean 4.9 probe-API defect and
  failed closed. After the compatibility fix, the worker completed through the
  independent gate.
- A Claude worker proved `autoformClaudePilot (n : Nat) : n * 1 = n` after an
  initial permission-contract failure was fixed and retried.
- The first Codex jury attempts exposed an output-schema mismatch and wrote no
  scores. After the strict-schema fix, the three-axis jury completed with
  faithfulness 5, proof integrity 5, code quality 3, and a clean verdict.
- The usage ledger records the successful Codex worker as `backend=codex`,
  `status=proved`, with wall time and token usage.

The project was rebuilt again during this hardening pass. `lake build` passed,
and `#print axioms` reported that both pilot theorems depend on no axioms.

## Native delegation result: not passed

Two read-only `codex exec` probes asked for the generated `autoform_reader`
role:

1. With `--ephemeral`, the collaboration router reported that the parent thread
   did not exist. The model nevertheless returned a plausible success object.
2. Without `--ephemeral`, the JSONL stream contained a wait operation with an
   empty receiver set and no child-thread result. The model again returned a
   plausible success object.

Neither run is evidence of native delegation. The release gate remains open
until a trusted project-rooted Codex task produces an actual child id, a
non-empty receiver set, and a terminal result from the requested generated
role. Model self-report is explicitly insufficient.

## Not yet run

- trusted interactive Codex setup/plan delegation;
- a live Claude jury and trusted interactive Claude setup/plan check;
- credentialed Aristotle, OpenAI, or Avocado probes; and
- cold Mathlib cache retrieval on a clean machine.

These remain opt-in because they may use subscription capacity, incur provider
cost, download substantial artifacts, or send selected project data to an
external service.
