---
name: setup
description: >-
  Set up, repair, or inspect an AutoformBot Lean formalization project's
  repository and environment. Handles Autoform and Lean prerequisites,
  Lean/Mathlib project creation, workspace status, durable-state
  initialization, the review dashboard, host roles, CI, GitHub Pages, and
  distributed-mode readiness. Use for setup, install, initialize, repair,
  inspect workspace, or prepare GitHub. Building the roadmap DAG itself
  belongs to the Roadmap workflow.
---

# Set up an Autoform project

Use the current host's native tools and subagents.

## Resolve the plugin root

Resolve one absolute plugin root. Prefer a valid `AUTOFORM_PLUGIN_ROOT`,
`MUSE_PLUGIN_ROOT`, `PLUGIN_ROOT`, or `CLAUDE_PLUGIN_ROOT`; otherwise use
`Path(<this loaded SKILL.md>).resolve().parents[2]`. The result must be the
directory containing `scripts/`, `skills/`, and `internal/`.

Print the resolved plugin root and target project. Stop if the root does not
contain both `scripts/merge_node.py` and `internal/runbooks/planning.md`.
In every command below, replace `<AUTOFORM_PLUGIN_ROOT>` with that quoted
absolute path; do not depend on a variable exported by a previous shell call.

## Start with a run brief

Before installing, creating, or planning anything, tell the user what this run
will do in a compact brief:

- the resolved Lean repository and plan/roadmap directory;
- whether this is a fresh install, a repair, or inspection only;
- the artifacts Setup will create or update (the Lean project, empty durable
  state, the lightweight dashboard, and — only with approval — CI, Pages, and
  gitignore entries);
- that Lean source remains editable in the user's IDE and code review remains
  ordinary GitHub branches, PRs, and CI; Autoform does not replace either;
- the next step after readiness: run Roadmap to build the dependency graph
  (Setup never reads sources or spawns planning subagents).

Do not make the user infer whether agents have started, which files they may
touch, or whether a backend is already spending tokens. Report those transitions
when they occur.

## Procedure

1. Check or repair the environment when this is a first run, when the user asks
   to install/repair Autoform, or when a required command is missing:

   ```bash
   bash "<AUTOFORM_PLUGIN_ROOT>/scripts/install_autoform.sh"
   ```

   The script checks uv, Python dependencies, Lean, and optional Zulip access.
   If `lean`, `lake`, or `elan` is still missing and the requested operation
   needs Lean, run:

   ```bash
   bash "<AUTOFORM_PLUGIN_ROOT>/scripts/install_lean.sh"
   ```

   These are internal Setup operations, not separate user commands.

2. Resolve the target project from an explicit argument, otherwise the current
   directory. If there is no `lakefile.*`, create the Lean project as an
   internal setup step:

   - Run the internal Lean installer first only when `lake` or `elan` is
     missing.
   - Ask for a project name in UpperCamelCase (for example `ConvexBodies`) and
     an optional target directory when they were not supplied.
   - Require a target directory that does not already exist, then run:

     ```bash
     bash "<AUTOFORM_PLUGIN_ROOT>/scripts/make_project.sh" \
       <ProjectName> [target-dir]
     ```

   - Resolve and echo the newly created directory as `PROJECT_DIR`.

   Project creation is deliberately bundled into Setup rather than exposed as
   a separate user-facing command.

   Resolve `DISPATCH_PROJECT` from an explicit plan directory, otherwise use the
   Lean project itself. Create an explicitly requested missing plan directory
   only after echoing both absolute paths.

3. For an existing project, inspect its current state before changing it:

   ```bash
   uv run --directory "<AUTOFORM_PLUGIN_ROOT>" python \
     "<AUTOFORM_PLUGIN_ROOT>/scripts/workspace_inspector.py" "$PROJECT_DIR"
   ```

   Use `--search`, `--declarations`, or `--targets` when the request is
   specifically about code search, declarations, or target status. Treat this
   inspection as the resume source of truth.

   Then ensure the project actually builds — a broken build silently blocks
   every downstream stage (the prover's verification gate, CI, and the
   auto-merge gate all depend on `lake build` succeeding):

   ```bash
   cd "$PROJECT_DIR" && lake exe cache get && lake build
   ```

   `lake exe cache get` is cheap when the cache is already local and may be
   skipped for projects without a Mathlib dependency. A build failure is a
   Setup failure to surface and fix now (toolchain mismatch, stale manifest,
   broken import), never something to defer to Roadmap or Orchestrate. Report
   how long the first build is likely to take when the cache is cold.

4. On Codex, install the canonical role agents into the project:

   ```bash
   uv run --directory "<AUTOFORM_PLUGIN_ROOT>" python \
     "<AUTOFORM_PLUGIN_ROOT>/scripts/install_host_agents.py" install \
     --host codex --project "$PROJECT_DIR"
   ```

   These project TOMLs are an optimization for Codex surfaces that expose
   custom-role selection. A newly created project's agents may not be visible
   in the task performing setup: tell the user to open a new Codex task rooted
   and trusted in the project. Until then—and whenever the native spawn tool
   has no role selector—spawn a generic native Codex subagent and include the
   full canonical `agents/<role>.md` body in its task. Claude Code reads those
   canonical plugin agents directly.

   On Muse, do not install Codex TOMLs. Muse's public plugin manifest does not
   expose an `agents` capability. Spawn a generic native Muse subagent and put
   the complete canonical `agents/<role>.md` body in its task, together with the
   absolute plugin root and project paths.

5. Initialize durable planning state without overwriting existing work:

   ```bash
   uv run --directory "<AUTOFORM_PLUGIN_ROOT>" python \
     "<AUTOFORM_PLUGIN_ROOT>/scripts/init_plan.py" \
     --project "$DISPATCH_PROJECT" --lean-root "$PROJECT_DIR"
   ```

   `DISPATCH_PROJECT` is the directory that owns `graph.json`; it may equal the
   Lean project or be a dedicated plan directory. The helper records the
   absolute Lean root in graph metadata.

   Setup only initializes durable state; growing, re-planning, or resetting
   the graph (`--reset-plan`) belongs to the Roadmap workflow.

6. Start the dashboard so the (initially empty) graph is visible and later
   Roadmap/Orchestrate activity appears live. For a fresh repository, or when
   its intended durable/local-state layout is unclear, read
   `<AUTOFORM_PLUGIN_ROOT>/skills/setup/references/worked-repository.md`.
   It explains the small worked asset under
   `skills/setup/assets/worked-formalization-project/`. Treat that asset as a
   teaching example, not a scaffold to copy wholesale: preserve the target
   repository's toolchain and conventions, and adapt only the
   durable/local-state boundaries.

   Roadmap owns source inspection and graph construction.

   Use a free
   loopback port unless the user supplied one. Reuse a service already serving
   this exact project.

   On macOS (including Codex desktop and Claude Code), always install the
   project-scoped launchd service:

   ```bash
   uv run --directory "<AUTOFORM_PLUGIN_ROOT>" python \
     "<AUTOFORM_PLUGIN_ROOT>/scripts/service_control.py" start review \
     --project "$DISPATCH_PROJECT" --plugin-root "<AUTOFORM_PLUGIN_ROOT>" \
     --graph "$DISPATCH_PROJECT/graph.json" --lean-root "$PROJECT_DIR" \
     --port <PORT_OR_0>
   ```

   This command is idempotent, binds only to `127.0.0.1`, persists after the
   assistant task ends, and automatically restarts after an unexpected exit.
   It prints the actual URL and keeps logs under
   `$DISPATCH_PROJECT/.autoform/logs/`. Do not substitute `nohup` on macOS:
   assistant hosts may reap task-owned background processes.

   On a non-macOS host, run `serve_review.py` as a foreground process using the
   host's durable service facility. If none is available, the existing
   `nohup ... serve_review.py ... &` command is an explicitly session-scoped
   fallback; tell the user it will need to be restarted after the host session
   ends. In every case, verify the loopback port is listening before reporting
   the URL.

7. Inspect GitHub publication readiness without changing remote or repository
   state:

   ```bash
   uv run --directory "<AUTOFORM_PLUGIN_ROOT>" python \
     "<AUTOFORM_PLUGIN_ROOT>/scripts/configure_github_pages.py" \
     --repo-root "$PROJECT_DIR" inspect
   ```

   The local dashboard remains the default. Do not add GitHub Pages merely
   because a Git remote exists. When the user asks for publication, show the
   inspection result and the exact contract before requesting approval:

   - files to add: `.autoform/pages.json` and
     `.github/workflows/autoform-pages.yml`;
   - published: graph structure, theorem content, proof status, review verdicts,
     and kernel evidence;
   - excluded: agent activity, task queues, dispatcher logs, backend settings,
     credentials, and local filesystem paths.

   Refuse automatic publication when the GitHub repository or its visibility is
   unclear. For a private or internal repository, require the user to verify
   that their GitHub plan or enterprise configuration provides the intended
   Pages access control before continuing. After explicit publication approval,
   run `configure_github_pages.py install --approve-publication` with the graph
   and site paths relative to the Git repository. Add
   `--private-pages-verified` only after that verification.

   Approval to add configuration does not authorize creating a repository,
   pushing commits, enabling Pages, or opening the deployed URL. Obtain separate
   explicit approval immediately before each requested outward-facing action.
   GitHub Pages is a committed-state snapshot: the exporter refuses dirty
   durable inputs, and updates occur only after those inputs are committed and
   pushed. Read and follow
   `<AUTOFORM_PLUGIN_ROOT>/internal/runbooks/github-pages.md` for the commands and
   failure rules.

8. When the user wants multiple machines or teammates advancing this project
   (a shared GitHub roadmap), prepare distributed mode:

   - audit the machine: `uv run --directory "<AUTOFORM_PLUGIN_ROOT>" python -m
     autoform_worker doctor --json` — surface every failing check;
   - the project needs a GitHub repository with an `origin` remote. When one
     is missing, DRIVE the creation rather than handing the user commands: ask
     one concrete question ("create private `<owner>/<name>` on GitHub and
     push this project? [yes/no]" — default to private, name it after the
     project directory), and on yes run `git init` + commit + `gh repo create
     <owner>/<name> --private --source . --push` yourself, then continue
     without further ceremony. The consent boundary is the question, not the
     typing: AutoformBot still never creates, pushes, or configures a
     repository silently, and approval for this repo does not carry over to
     any later outward-facing action (Pages, CI enablement, publication);
   - check whether the repository verifies its pull-request heads. Autonomous
     merging requires at least one real check: with no workflows, the
     auto-merge gate stays shut by design. When the user wants autonomous
     merging and the repo has no build check, offer to copy
     `<AUTOFORM_PLUGIN_ROOT>/templates/github/autoform-verify.yml` into
     `.github/workflows/` (substituting `__DEFAULT_BRANCH__`) — it builds the
     project, rejects surviving `sorry`/`admit`, and audits axioms, mirroring
     the local prover gate on neutral hardware. Adding the file is a local
     edit; committing and pushing it needs separate explicit approval;
   - ensure the Lean repo's `.gitignore` keeps per-machine state local while
     durable state stays committed. Local-only: `task_queue.json`,
     `agents_status.json`, their `*.lock` files, `dispatch.log`, `worker.log`,
     `.autoform/`.
     Committed: `graph.json`, `informal_content/`, `kernel/`,
     `review_status.json`, the Lean sources;
   - if the user wants cross-machine escalation visibility, note whether
     Issues are enabled on the canonical repo (forks disable them by default;
     enabling is a repo-settings action the user performs).

   Distributed operation itself (rounds, claims, PRs) belongs to Orchestrate;
   Setup only makes the machine ready.

9. Report the local dashboard URL, GitHub publication readiness, native
   role-agent install status, distributed-mode readiness when configured, and
   the next step: run Roadmap when the graph is empty, otherwise Orchestrate.

## Resume semantics

Derive readiness from `graph.json`; do not rely on chat history. A crash loses
only in-flight subagent work. Completed merges, queue entries, verdicts, and
content files remain authoritative.
