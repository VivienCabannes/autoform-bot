# Codex support

Codex is a first-class Autoform host and prover backend.

The earlier version of this document treated skills, MCP servers, and native
subagents as unconfirmed or absent in Codex. That premise is obsolete. Current
Codex releases support plugin skills and MCP servers, native parallel subagents,
project-scoped custom agents in `.codex/agents/*.toml`, steering, resumable work,
and structured headless output.

## What parity means

Claude Code and Codex run the same Autoform skills against the same durable
artifacts:

- `graph.json` and node prose;
- `task_queue.json` and the activity feed;
- `review_status.json` and shared rubric thresholds;
- the unified prover driver, event model, steering policy, and Lean verification
  gate.

Each host still uses its native execution surface. Claude consumes the plugin's
`agents/*.md`. Autoform's setup installs equivalent namespaced role agents under
the target project's `.codex/agents/` for Codex. When the active Codex spawn
surface has no custom-role selector—or the current task predates installation—
the orchestrator spawns a generic native subagent with the complete canonical
role prompt inlined. It never launches `codex exec` or `claude -p` to imitate
native delegation.

Headless work is separate. `dispatch_runner.py` can use Claude or Codex for the
jury and Claude, Codex, Aristotle, OpenAI, or Avocado for proof workers. Those
adapters normalize results at Autoform's existing contracts; they do not replace
the native interactive workflow.

## Install the Codex role agents

From the Autoform plugin root:

```bash
uv run python scripts/install_host_agents.py install \
  --host codex --project /path/to/LeanProject
```

The command is idempotent and preflights the complete install before writing. It
updates or removes only files carrying Autoform's generated marker and refuses
to overwrite user-managed agent files. Start a new Codex task rooted and trusted
in the project to discover newly installed roles; prompt inlining covers the
setup task itself.

## Permission model

Codex workers default to `workspace-write`; jury processes use `read-only`.
Autoform does not use `--dangerously-bypass-approvals-and-sandbox` unless
`AUTOFORM_UNSAFE_FULL_ACCESS=1` is explicitly set.

This is an authority reduction, not a sufficient isolation boundary for a
hostile Lean repository: Lean elaboration and build scripts can execute code
while a proof worker iterates. Use an external VM/container or equivalent
sandbox for untrusted projects.

## Known external boundary

Codex custom agents are documented at project/user scope, not as a plugin
manifest field. Autoform therefore materializes the role TOML files during
setup. If Codex adds documented plugin-bundled agent configuration, the
generator can become a compatibility fallback.

See [full-parity-architecture.md](full-parity-architecture.md) for the complete
host/provider split, contracts, data-egress model, and validation matrix.
