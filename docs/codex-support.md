# Codex implementation and release status

Autoform contains first-class Codex host and prover implementations. That is a
code-status statement, not a blanket claim that every Codex version, account,
permission profile, or project has passed a live Autoform run.

The earlier version of this document treated skills, MCP servers, and native
subagents as unconfirmed or absent in Codex. That premise is obsolete. Current
Codex documentation covers plugin skills and MCP servers, native parallel
subagents, project-scoped custom agents in `.codex/agents/*.toml`, resumable
work, and structured `codex exec` output.

## Status boundary

| Surface | Implemented and automated | Live release gate |
|---|---|---|
| Three-command workflow surface and MCP registration | Plugin/static validation enforces exactly `setup`, `roadmap`, and `orchestrate` | Install the built plugin in the target Codex surface and confirm only those three Autoform skills plus the MCP tools are discovered |
| Project Codex role agents | Idempotent generator, overwrite protection, and isolated-project tests | Start a new trusted project task and spawn at least one generated role |
| Native interactive orchestration | Canonical role prompts and generic-subagent fallback | Complete setup/plan and one escalation or review delegation in the target Codex version; require child-thread evidence rather than a model self-report |
| Headless Codex prover | CLI arguments, event normalization, timeout cleanup, steering, and fake-process tests | Passed on the 2026-07-27 disposable pilot; repeat on the final release SHA |
| Headless Codex jury | Shared schema, abstention rules, and fake-process tests | Passed on the 2026-07-27 three-axis pilot; repeat on the final release SHA |

Accordingly, release notes should say **Codex implementation complete** until
the live gates in [pilot-testing.md](pilot-testing.md) have been recorded for
the release candidate. Say **end-to-end Codex validated** only after those
gates pass, including the Codex version, permission profile, Lean toolchain, and
whether the run incurred billing or data egress.

The dated evidence and remaining native-delegation blocker are recorded in
[pilot-results-2026-07-27.md](pilot-results-2026-07-27.md).

## What parity means

Claude Code and Codex run the same three Autoform workflows against the same
durable artifacts. Planning, visualization, review, workspace inspection,
proof discipline, and Zulip guidance are non-discoverable runbooks reached
through Setup, Roadmap, or Orchestrate rather than additional commands.

- `graph.json` and node prose;
- `task_queue.json` and the activity feed;
- `review_status.json` and shared rubric thresholds;
- the unified prover driver, event model, steering policy, and Lean verification
  gate.

Each host still uses its native execution surface. Claude consumes the plugin's
`agents/*.md`. Autoform's setup installs equivalent namespaced role agents under
the target project's `.codex/agents/` for Codex. When the active Codex spawn
surface has no custom-role selector—or the current task predates installation—
the orchestrator instructs the host to spawn a generic native subagent with the
complete canonical role prompt inlined. This is the documented fallback design;
the live release gate verifies that the target Codex surface follows it. The
interactive workflow never shells out to `codex exec` or `claude -p` to imitate
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
