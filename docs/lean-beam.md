# Lean Beam integration

Autoform delegates Lean process ownership and protocol handling to the
Lean-FRO-maintained preview in
[`leanprover/lean-beam`](https://github.com/leanprover/lean-beam).
Autoform does not proxy Lean LSP, parse REPL output, or maintain a second pool
of Lean processes. One long-lived Beam stdio process owns the reusable Lean
runtimes; explicit workspace descriptors, source snapshots, and proof handles
carry all state that matters to callers.

The supported development revision and protocol are recorded in
[`lean-beam.lock.json`](../lean-beam.lock.json). Install that exact revision
from a clean checkout with Lean Beam's own installer. This example registers
the canonical server for Codex.

```bash
git clone https://github.com/leanprover/lean-beam.git
cd lean-beam
git checkout --detach d5dc8fe9d3928899bf55968a93d9e309d9fad1bc
./scripts/install-beam.sh --dont-ask \
  --toolchain leanprover/lean4:v4.32.2 \
  --toolchain leanprover/lean4:v4.33.0 \
  --codex-mcp
```

Autoform does not bundle a Beam executable, wrap its protocol, or register a
second MCP server. Use Beam's own installer to register the canonical
`lean-beam` server for the desired host. The command above uses `--codex-mcp`;
use `--claude-mcp` for Claude Code, or both flags for both hosts. Do not install
Beam's companion agent skill while Autoform excludes its save operations; this
document is the preview workflow contract. Restart each configured host
afterward. The setup workflow and CI verify the running process through the
typed `beam_version` tool; a caller that skips that check has not established
provenance.

Lean Beam does not yet publish a Muse MCP registration path. Autoform's native
Muse skills remain available, but Lean tooling in this preview is unsupported
there; do not silently substitute the removed Autoform servers.

This pin is an exact commit from a draft pull request and is for integration
development, not release. Autoform must not ship the cutover until Lean Beam
publishes a tagged release containing the opaque source-snapshot work from
[`leanprover/lean-beam#254`](https://github.com/leanprover/lean-beam/pull/254),
its release CI is green, and the save and external-build synchronization
defects in
[`#255`](https://github.com/leanprover/lean-beam/issues/255) and
[`#256`](https://github.com/leanprover/lean-beam/issues/256) are resolved in
that release. Setup also needs a public typed way to verify the selected
workspace toolchain and bundle, tracked in
[`#257`](https://github.com/leanprover/lean-beam/issues/257).
Until then, CI additionally evaluates `Lean.versionString` inside each test
workspace. That proves which compiler served the request, but it is not a
typed workspace-identity API for agents.

## Explicit state model

Every workspace-bound call carries an absolute workspace descriptor:

```json
{"workspace":{"root":"/absolute/path/to/project"}}
```

Use the Beam API in this order:

1. Call `lean_sync` for a saved Lean file and retain its opaque `snapshot`.
2. Use `lean_run_at` for one isolated command or tactic block.
3. When continuation matters, use `lean_run_at_handle`, then prefer the
   non-linear `lean_run_with`. Advance to its `next_handle` only after semantic
   success, then release the parent. `lean_run_with_linear` consumes its parent
   before execution, so reserve it for deliberate consume-on-attempt flows.
4. Release unused handles with `lean_release`.
5. After a real source edit, save it and call `lean_update` or `lean_sync` for a
   fresh snapshot. Never substitute a fresh token into stale coordinates.
6. Use `lean_close` for one document and `lean_drop_workspace` after project
   configuration changes or when a workspace generation must be discarded.

Position fields use zero-based LSP line and character coordinates. Characters
are counted in UTF-16 code units, not Unicode scalar values or UTF-8 bytes.

There is no Autoform session id and no hidden continuation between calls. The
workspace root, saved source file and path, source snapshot, and proof handles
are the complete public state. Snapshots and handles are invalid after a source
edit, document close, backend or MCP restart, or workspace drop; synchronize
again instead of carrying either token across those boundaries.

The workspace descriptor routes a request; it is not a filesystem
authorization boundary. Beam permits absolute paths outside that root so it
can inspect dependency sources. Autoform adds no policy proxy, so use the
preview only where the MCP process and caller already share a trusted local
filesystem. Before release, Autoform's older project-root admission requirement
must either be revised explicitly or implemented upstream as a public Beam
policy.

`lean_run_at` accepts one top-level command or one tactic block. It is not a
replacement for the old arbitrary multi-command `run_lean_code` call, and it
cannot introduce imports. Put imports and multi-command changes in the saved
source file, then synchronize it. Autoform does not split Lean source with a
home-grown parser. Native multi-command speculation is tracked in
[`leanprover/lean-beam#100`](https://github.com/leanprover/lean-beam/issues/100).

Do not automatically retry a speculative command after cancellation, timeout,
or transport loss. Its result is unknown. Discard the affected workspace
generation or restart the MCP process, reread the file, synchronize it, and
resume from a fresh snapshot. Beam isolates document state, but Lean code and
metaprogramming can still perform arbitrary IO; this is not an operating-system
sandbox.

Beam cancellation is cooperative. Autoform does not supervise the Beam
process or add a timeout or retry layer. An MCP host that needs a hard deadline
must terminate the Beam MCP process itself if cancellation does not settle;
doing so invalidates every handle owned by that process.

Until the upstream save issues are closed, Autoform workflows use `lean_sync`
for the interactive diagnostics barrier and a clean external `lake build` for
final verification. They do not call `lean_save` or `lean_close_save`; the
direct Beam server still exposes those tools, so this is a workflow rule rather
than a technical filter. Until `leanprover/lean-beam#256` is fixed, any external
`lake build` performed while the MCP process is alive must be followed by
`lean_drop_workspace` before the next Beam operation; the next call recreates
the workspace from disk.

Before changing `development_only` to false, integration CI must also cover
request cancellation followed by host-imposed hard process teardown, and the
supported hosts must provide one caller-visible request deadline. These are
release criteria, not properties inferred from the normal success-path smoke
test.
