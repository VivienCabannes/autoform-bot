# Lean servers

Autoform's four Lean tools require an absolute `project_dir`, so the plugin
directory is never mistaken for the user's Lean project. Stateless Mathlib or
community search stays outside the server surface and uses host-native tools
when useful.

## Shared runtime

Plugin hosts start the two stdio MCP processes automatically. They are
lightweight adapters: the first Lean tool call race-safely starts a detached
runtime for the current AutoformBot installation, Unix user, and compute node.
That runtime owns one resident REPL pool and LSP session per active Lean
project, so sessions using that installation reuse the same warmed processes.
Closing the session that started it does not stop it; after a crash, the next
tool call starts it again. Runtime sockets include a code fingerprint, so an
in-place upgrade gracefully replaces the older build.

REPL and LSP processes remain lazy. A cold tool call stays pending while Lean
warms up, so no `/repl-start`, `/lsp-start`, or model-side sleep is needed. Idle
project processes are closed after 30 minutes by default, while the small
runtime remains available. Its lifecycle is also explicit:

```bash
uv run autoform-lean-runtime start
uv run autoform-lean-runtime status
uv run autoform-lean-runtime stop
```

`stop` is graceful: it waits for admitted tool calls and Lean children to
finish shutting down before a subsequent `start` can replace the runtime.

The private socket lives below `$XDG_RUNTIME_DIR/autoform`, falling back to a
uid-specific directory in `/tmp`; the rotating runtime log is beside it.
`AUTOFORM_RUNTIME_DIR` overrides that location. Node-wide limits are controlled
by `AUTOFORM_REPL_TOTAL_WORKERS`, `AUTOFORM_REPL_WORKERS_PER_PROJECT`,
`AUTOFORM_REPL_MAX_CONTEXTS_PER_PROCESS`, `AUTOFORM_MAX_LEAN_PROJECTS`, and
`AUTOFORM_LEAN_IDLE_SECONDS`. Each worker retains at most one structured-import
environment and restarts when its context budget, 256 by default, is reached.
The first process to start the runtime supplies those settings until it is
stopped.
`AUTOFORM_REPL_REQUEST_TIMEOUT` sets the default end-to-end REPL call budget
(180 seconds), bounded by `AUTOFORM_MAX_REPL_REQUEST_SECONDS`. The client-side
`AUTOFORM_RUNTIME_RESPONSE_TIMEOUT` must remain above the node-wide request
limits.

`run_lean_code` accepts an optional ordered `imports` list for project modules
that Lake has already built. Nonempty lists are validated against the project;
each worker reuses only an exact, still-current import environment.
