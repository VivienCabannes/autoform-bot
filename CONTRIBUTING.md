# Contributing to Autoform

Autoform is a host-neutral Lean formalization plugin. Claude Code and Codex use
the same Agent Skills, durable graph/queue formats, prover contracts, jury
schemas, and verification gate. Changes should preserve that shared contract
instead of adding host-specific workflow copies.

Read `docs/full-parity-architecture.md` before changing orchestration, provider
adapters, permissions, or plugin packaging.

Version 2 intentionally replaces the former standalone Python application.
Read `docs/migration-v2.md` before restoring a v1 module, command, dependency,
or run format; compatibility work must define an explicit artifact boundary
rather than silently creating a second orchestration stack.

## Current surfaces

| Area | Location | Status |
|---|---|---|
| Portable workflow skills | `skills/` | Implemented |
| Claude canonical agents | `agents/*.md` | Implemented |
| Generated Codex agents | `scripts/install_host_agents.py` | Implemented |
| Durable plan/queue/review control plane | `scripts/` | Implemented |
| Unified prover and verification gate | `servers/prover/` | Implemented |
| Mathlib and Zulip MCP servers | `servers/mathlib/`, `servers/zulip/` | Implemented |
| Aristotle MCP/server adapter | `servers/aristotle/` | Implemented, optional dependency |
| REPL and LSP MCP servers | `servers/repl/`, `servers/lsp/` | Stubs; reference implementations are under `examples/servers/` |
| Claude/Codex packaging | `.claude-plugin/`, `.codex-plugin/`, `.mcp.json`, `hooks/hooks.json` | Implemented |

Useful next contributions include production REPL/LSP implementations, an
OpenAI Responses transport beside Chat Completions, live opt-in provider
contract tests, and additional adversarial verification fixtures.

## Compatibility rules

- Put user-invocable workflows in `skills/<name>/SKILL.md` with only portable
  `name` and `description` frontmatter.
- Keep canonical role instructions in `agents/*.md`. Update the Codex generator
  or its tests when the role contract changes; do not hand-maintain a second
  prompt.
- Keep graph writes behind `scripts/merge_node.py` and queue writes under the
  existing lock/atomic-save discipline.
- Add provider behavior behind the adapter and normalized event contracts. Do
  not branch the shared driver on provider names.
- Treat model output, repository text, tool arguments, paths, and Lean source as
  untrusted data. Unknown providers and policies must fail closed.
- Never add prompt-triggered shell execution. Session hooks may provide static
  context but must not interpolate user prompts into commands.
- Do not add guessed private endpoint URLs, model ids, authentication schemes,
  or data-egress consent.
- Preserve the independent Lean build, forbidden-execution scan, and axiom
  audit for every proved claim.

## Adding or changing an MCP server

Keep pure logic in `core.py` and the thin FastMCP wrapper in `server.py`. Optional
SDK imports must remain lazy so plugin discovery works without every extra
installed.

Register a shared server consistently in:

- `.claude-plugin/plugin.json`;
- `.mcp.json`;
- `scripts/lint_plugin.py`'s expected-server contract.

`scripts/lint_plugin.py` enforces the current shared server set. Add import,
factory, path-confinement, and fake/injected behavior tests; CI must not require
network access or paid credentials.

## Adding a provider

Implement `ProverAdapter`, normalize events and usage, declare the correct
`SteeringCapability`, and let `servers/prover/driver.py` apply the common
verification gate. Add exact backend validation, configuration-only preflight,
explicit data-egress consent where network project data is involved, and
transport-injected tests.

For a live test, use a temporary disposable Lean project and require an explicit
flag. Never make a credentialed or billable request in the default suite.

## Adding or changing a skill

Make the skill independently executable from its loaded `SKILL.md`: resolve the
plugin root explicitly, use absolute paths in commands, define durable resume
semantics, and name the native-agent fallback when a host lacks role selection.
Do not rely on shell state from an earlier command or duplicate the workflow in
legacy `commands/`.

Validate every skill:

```bash
uv run python /path/to/skill-creator/scripts/quick_validate.py skills/<name>
```

## Validation

From the repository root:

```bash
uv sync --extra dev --extra repl --extra zulip
uv run python -m pytest -q
python3 scripts/lint_plugin.py
claude plugin validate .
uv run ruff check <changed-python-files>
git diff --check
```

The default tests require no paid credentials and make no external network
requests. They use fake CLIs/transports, a loopback HTTP server, and—when Lake is
installed—small temporary real-kernel smoke projects. Document any
environment-specific live check separately, including the CLI version and
whether it could incur billing or disclose project data. The complete checklist
is in `docs/pilot-testing.md`.

## Pull requests

Keep each pull request centered on one contract change. Include tests for both
the success path and the fail-closed path, update architecture/user docs when
behavior changes, and call out any remaining live-validation gap honestly.
