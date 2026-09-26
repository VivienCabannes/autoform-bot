# Lean servers

Autoform's four Lean tools require an absolute `project_dir`, so the plugin
directory is never mistaken for the user's Lean project. Stateless Mathlib or
community search stays outside the server surface and uses host-native tools
when useful.

## Shared runtime

Plugin hosts start the two stdio MCP processes automatically. They are
lightweight adapters: the first Lean tool call race-safely starts a detached
runtime for the current AutoformBot installation, Unix user, and compute node.
That runtime owns one REPL admission pool and LSP session per active Lean
project. Every public REPL call starts a fresh child and reaps it before
returning, so environments, proof states, and stream contents cannot cross
independent successful calls. If cleanup cannot be confirmed, Autoform returns
an explicit no-replay error, quarantines that project pool, and blocks its
replacement until cleanup succeeds. LSP sessions remain resident because their
protocol is explicitly stateful. Closing the session that started the runtime
does not stop it; after a crash, the next tool call starts it again. Runtime
sockets include a code fingerprint, so an in-place upgrade gracefully replaces
the older build.

Lean subprocesses remain lazy. A REPL call stays pending while its fresh child
starts, and the first LSP call stays pending while its session starts, so no
`/repl-start`, `/lsp-start`, or model-side sleep is needed. Idle project slots
and LSP sessions are closed after 30 minutes by default, while the small runtime
remains available. The runtime currently requires POSIX process groups and Unix
domain sockets; unsupported platforms fail before starting Lean. Its lifecycle
is also explicit:

```bash
uv run autoform-lean-runtime start
uv run autoform-lean-runtime status
uv run autoform-lean-runtime stop
```

`stop` is graceful: it waits for admitted tool calls and Lean children to
finish shutting down before a subsequent `start` can replace the runtime.

REPL transport retries are limited to failures detected before the complete
request frame is dispatched. Once the final frame delimiter may have reached
Lean, replay could execute the command twice, so Autoform retires the process
and reports that the outcome is unknown instead of retrying.
The REPL per-call timeout starts before the shared daemon is connected or
started, then covers project admission, fresh child startup, idle-worker wait,
and Lean execution. Verified process cleanup and response delivery get a
separate bounded grace period before the RPC returns.

`LEAN_REPL_CMD` is a trusted local command. Its descendants must remain in the
dedicated process group Autoform creates; a command that deliberately detaches
with a new session escapes that operating-system cleanup boundary.

The private socket lives below `$XDG_RUNTIME_DIR/autoform`, falling back to a
uid-specific directory in `/tmp`; the rotating runtime log is beside it.
`AUTOFORM_RUNTIME_DIR` overrides that location. Node-wide limits are controlled
by `AUTOFORM_REPL_TOTAL_WORKERS`, `AUTOFORM_REPL_WORKERS_PER_PROJECT`,
`AUTOFORM_MAX_LEAN_PROJECTS`, and `AUTOFORM_LEAN_IDLE_SECONDS`. The first
process to start the runtime supplies those settings until it is stopped.
`get_repl_status` reports a project pool as `warm` when its admission slots are
cached; it does not mean a Lean REPL child is resident between calls.
`AUTOFORM_RUNTIME_RESPONSE_TIMEOUT` can raise the client/daemon response budget
when a Lean operation and its verified child cleanup need more than the default
15 minutes.
