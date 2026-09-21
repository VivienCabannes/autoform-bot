# Lean servers

Autoform's four Lean tools require an absolute `project_dir`, so the plugin
directory is never mistaken for the user's Lean project. Stateless Mathlib or
community search stays outside the server surface and uses host-native tools
when useful.

## Shared runtime

Plugin hosts start the two stdio MCP processes automatically. They are
lightweight adapters: the first Lean tool call race-safely starts a detached
runtime for the current AutoformBot installation, Unix user, and compute node.
That runtime keeps bounded, project-scoped REPL admission slots and one LSP
session per active Lean project. Every `run_lean_code` call starts a fresh REPL
child and reaps it before returning, so declarations, environment identifiers,
proof-state identifiers, and stream state cannot cross calls. LSP sessions stay
warm because their protocol is explicitly stateful. Closing the plugin session
that started the runtime does not stop it; after a crash, the next tool call
starts it again. Runtime sockets include a code fingerprint, so an in-place
upgrade gracefully replaces the older build.

Lean subprocesses remain lazy. A REPL call stays pending while its fresh child
starts, and the first LSP call stays pending while its session starts, so no
`/repl-start`, `/lsp-start`, or model-side sleep is needed. Idle project
admission slots and LSP sessions are removed after 30 minutes by default, while
the small runtime remains available. Its lifecycle is also explicit:

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
`AUTOFORM_MAX_LEAN_PROJECTS`, and `AUTOFORM_LEAN_IDLE_SECONDS`. The first
two settings bound concurrent disposable REPL children across the node and per
project; they do not keep those children resident. The first process to start
the runtime supplies these settings until it is stopped.
`AUTOFORM_REPL_REQUEST_TIMEOUT` sets the default end-to-end REPL call budget
(180 seconds), bounded by `AUTOFORM_MAX_REPL_REQUEST_SECONDS`. The client-side
`AUTOFORM_RUNTIME_RESPONSE_TIMEOUT` must remain above the node-wide request
limits.

`run_lean_code` accepts an optional ordered `imports` list for project modules
that Lake has already built. Nonempty lists are validated against the project.
Every call, with or without structured imports, executes in one fresh REPL
process that is retired before the result is returned.

`get_repl_status` reports whether the project's admission pool is `cold`,
`warming`, or `warm`; `warm` describes the cached pool, not a resident Lean
process. Likewise, the daemon status's `resident` list contains cached project
pools. Its memory figure is zero while no request child is running.
