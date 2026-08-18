# PR #21 selective provider ports

PR [#21](https://github.com/VivienCabannes/autoform-bot/pull/21) explored full Claude, Codex, Muse, OpenAI-compatible, and Avocado provider parity. Its exact head is preserved at `archive/pr21-full-parity-59fb7956` (`59fb7956e3b139c0440c0daea1acb565ce9d577d`).

Do not merge or cherry-pick that branch wholesale. It is built around the retired `graph.json`, dispatcher, dashboard, and v2 migration architecture. Current Autoform uses Markdown roadmap articles, Git-ref claims, portable workflow skills, and the shared Lean LSP/REPL runtime.

## Already integrated or superseded

- Portable Setup, Roadmap, Orchestrate, review, and plugin-development skills.
- Claude, Codex, and Muse plugin manifests.
- Native LSP and REPL MCP exposure.
- Claim-backed worker coordination.
- Host-neutral agent definitions and review workflows.
- Minimal package and CI contracts.

## Selective ports

Each item below requires an independent current-architecture design and implementation PR.

### Provider adapters

Define bounded headless adapters for Claude, Codex, and Muse behind the current prover interface. Preserve provider-neutral requests and results; do not revive the old dispatcher or task schema.

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

Design a jury layer over current proof artifacts and review rubrics. A provider must not judge its own unverified output as the sole acceptance signal.

Required gates:

- Independent provider or deterministic verifier separation.
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

Build Muse packages in an isolated staging directory rather than directly from a mutable checkout.

Required gates:

- Exclude VCS metadata, virtual environments, caches, bytecode, credentials, and symlinks escaping the package.
- Validate the staged manifest before installation.
- Content-address or cache-bust builds so hosts cannot retain stale packages.
- Verify installed package contents against the staged artifact.

### Independent proof verification

A provider-written Lean proof must be checked outside the provider trust boundary before it can be accepted.

Required gates:

- Kernel-backed Lean verification through the current shared runtime or CI.
- Reject unsafe or partial declarations where the workflow requires kernel trust.
- Record the exact source, toolchain, imports, and verification result.
- Never treat provider self-report, successful text generation, or an untrusted HTTP response as proof validity.

## Security prerequisites

Before any port:

- Write a threat model covering credentials, untrusted repository content, model output, tool execution, and artifact publication.
- Require explicit egress consent per provider and endpoint.
- Validate all user-controlled provider, model, URL, command, and filesystem configuration.
- Keep Lean and shell execution outside provider-controlled processes.
- Add credential-leak, prompt-injection, path-traversal, timeout, and resource-exhaustion tests.
- Collect fresh pilot evidence; PR #21's July 2026 pilot results are historical only.
