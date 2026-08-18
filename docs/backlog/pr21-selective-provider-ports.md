# PR #21 selective provider ports

PR [#21](https://github.com/VivienCabannes/autoform-bot/pull/21) explored Claude, Codex, Aristotle, and OpenAI-compatible/Avocado provider parity. Its exact head is preserved at `archive/pr21-full-parity-59fb7956` (`59fb7956e3b139c0440c0daea1acb565ce9d577d`). Muse support was developed later on the separate 0.6.1 line, preserved at `archive/rework-exact-20260818` (`58867a2843964d25baa9c52a0e7fccf829ef5a81`).

Do not merge or cherry-pick either archive wholesale. PR #21 is built around the retired `graph.json`, dispatcher, stateful queue/dashboard control plane, and v2 migration architecture. Current Autoform retains rendered publication dashboards, but derives them from Markdown roadmap articles rather than mutable dispatcher state.

## Already integrated or superseded

- Main provides portable Setup, Roadmap, review, and plugin-development skills.
- The separate Deicyde overlay provides Orchestrate and worker execution; main intentionally excludes them.
- Main provides Git-ref claim primitives, but this does not imply that every Deicyde worker path enforces coordinated failure handling.
- Current host packages provide Claude, Codex, and Muse manifests independently of PR #21.
- Main exposes native LSP and REPL MCP services through the shared Lean runtime.
- Current branches provide host-neutral agent definitions, review workflows, and minimal package/CI contracts.

## Selective ports

Each item below requires an independent current-architecture design and implementation PR.

### Provider interface and adapters

First design a new provider-neutral request, result, cancellation, and error interface for the current architecture. Main has no prover interface today; do not adopt the archived `servers/prover` contract by default. After that design is reviewed, implement bounded headless adapters for the selected hosts without reviving the old dispatcher or task schema.

Required gates:

- Explicit startup, request, and shutdown timeouts.
- Process-tree cleanup and cancellation tests.
- Structured results instead of log scraping where supported.
- No provider credentials in command arguments, logs, prompts, or persisted artifacts.

### OpenAI-compatible and Avocado tool loops

Port only the bounded tool-loop concepts. Treat model output, tool arguments, and endpoint configuration as untrusted input.

Required gates:

- Strict tool allowlists and argument schemas.
- Total turn, token, wall-clock, and subprocess budgets.
- Explicit endpoint and egress approval.
- HTTPS and credential-redaction tests.
- No automatic replay after ambiguous tool execution.

### Provider-neutral jury execution

Design a jury layer over current proof artifacts and review rubrics. A provider must not judge its own output as the sole acceptance signal.

Required gates:

- Deterministic verification may establish compilation and proof integrity only.
- Source faithfulness, statement equivalence, and code-quality judgments require an independent reviewer or provider.
- Stable evidence bundles and score schemas.
- Bounded fan-out and cost accounting.
- Adversarial tests for prompt injection in candidate proofs and source material.

### Codex role generation

Generate project roles without interpolating untrusted text into TOML, shell commands, or prompts.

Required gates:

- Structured TOML serialization.
- Fixed role identifiers and destination confinement.
- Atomic writes that do not overwrite authored files without ownership evidence.
- Round-trip parser tests with quotes, newlines, and token-shaped text.

### Usage ledgers and budgets

Introduce an append-safe, machine-readable usage ledger for provider calls and worker rounds.

Required gates:

- Monotonic and finite numeric validation.
- Duplicate-key rejection for decision-bearing JSON.
- Atomic persistence and concurrent-writer tests.
- Explicit per-provider and per-project budget enforcement.

### Muse staging

This concept comes from the later archived 0.6.1 line, not PR #21. Build Muse packages in an isolated staging directory rather than directly from a mutable checkout.

Required gates:

- Exclude VCS metadata, virtual environments, caches, bytecode, credentials, and symlinks escaping the package.
- Validate the staged manifest before installation.
- Content-address or cache-bust builds so hosts cannot retain stale packages.
- Verify installed package contents against the staged artifact.

### Independent proof verification

A provider-written Lean proof must be checked outside the provider's trust boundary before it can be accepted. Process separation alone is not a security boundary: Lean elaboration and metaprogramming can execute with the invoking user's privileges.

Required gates:

- Evaluate provider-originated Lean in a sandbox with least privilege, restricted inputs, explicit command/tool allowlists, destination confinement, and bounded CPU, memory, process, filesystem, and network access.
- Perform kernel-backed verification and an explicit axiom audit; reject `sorryAx`, unauthorized axioms, unsafe declarations, and partial declarations regardless of whether verification runs through the shared runtime or CI.
- Record the exact source, toolchain, imports, allowed axioms, and verification result.
- Never treat provider self-report, successful text generation, compilation alone, or an untrusted HTTP response as proof validity.

## Security prerequisites

Before any port:

- Write a threat model covering credentials, untrusted repository content, model output, tool execution, Lean metaprogramming, and artifact publication.
- Require explicit egress consent per provider and endpoint.
- Validate all user-controlled provider, model, URL, command, and filesystem configuration.
- Run every provider-originated Lean or shell operation with sandboxing, least privilege, explicit tool and argument allowlists, restricted inputs, and destination confinement.
- Add credential-leak, prompt-injection, path-traversal, unauthorized-axiom, unsafe-declaration, timeout, and resource-exhaustion tests.
- Collect fresh pilot evidence. PR #21 contains only a pilot procedure; the later, incomplete historical result record is preserved at [`docs/pilot-results-2026-07-27.md`](https://github.com/VivienCabannes/autoform-bot/blob/58867a2843964d25baa9c52a0e7fccf829ef5a81/docs/pilot-results-2026-07-27.md) and must not be treated as a current release gate.
