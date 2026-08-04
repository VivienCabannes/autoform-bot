---
name: setup
description: >-
  Set up, repair, inspect, plan, visualize, or resume an Autoform Lean
  formalization project. Handles Autoform and Lean prerequisites, Lean/Mathlib
  project creation, workspace status, the durable DAG, blueprint, dashboard,
  and host roles. Use for setup, install, initialize, resume, reset, inspect
  workspace, plan a formalization, or view its graph.
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
- whether this is a fresh plan, a resume, or inspection only;
- the confirmed source files and exact chapter/section scope, or the specific
  missing information you need before planning;
- the artifacts Autoform will create or update (`graph.json`, prose, worker
  queue/status, and the lightweight dashboard);
- that Lean source remains editable in the user's IDE and code review remains
  ordinary GitHub branches, PRs, and CI; Autoform does not replace either;
- the next checkpoint the user should expect (coarse roadmap approval before
  detailed splitting, then explicit worker/prover dispatch).

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

   A request to "rebuild" does not authorize deletion: re-render the blueprint
   and resume the graph. Only an explicit user-confirmed plan reset authorizes
   adding `--reset-plan`. Before executing it, state that graph, prose, queue,
   reviews, and activity will be reset and that a timestamped snapshot will be
   retained under `<dispatch-project>/.autoform/snapshots/`.

6. Start the dashboard before planning so graph changes appear live. Use a free
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

7. Read and follow
   `<AUTOFORM_PLUGIN_ROOT>/internal/runbooks/planning.md`, including its schema
   at `<AUTOFORM_PLUGIN_ROOT>/internal/references/plan-json-schema.md`. Planning
   is incomplete when the graph is
   absent or empty, a tier-1 cluster has no tier-2 children, or a node has null
   content. Preserve every durable node already merged.

   Confirm source files and scope with the user before planning. Do not invent a
   source or silently widen the requested chapters. Use native subagents for the
   canonical roles (`splitter`, `mathlib-checker`, and reviewers), and route all
   graph edits through `scripts/merge_node.py`.

8. The lightweight dashboard from step 6 is the default visualization. Do not
   check for Graphviz, install blueprint Python packages, export leanblueprint,
   or build its web output during ordinary Setup. Those dependencies are not
   prerequisites for Autoform planning or orchestration.

   Only when the user explicitly requests the publication-style mathematical
   blueprint, read and follow
   `<AUTOFORM_PLUGIN_ROOT>/internal/runbooks/visualization.md` to export, build,
   and serve it. A blueprint toolchain failure must not turn an otherwise
   successful Setup run into a failure; report it as an optional visualization
   limitation and keep the graph and lightweight dashboard available.

9. Inspect GitHub publication readiness without changing remote or repository
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

10. When the user wants multiple machines or teammates advancing this project
   (a shared GitHub roadmap), prepare distributed mode:

   - audit the machine: `uv run --directory "<AUTOFORM_PLUGIN_ROOT>" python -m
     autoform_worker doctor --json` — surface every failing check;
   - the project needs a GitHub repository with an `origin` remote. Autoform
     never creates, pushes, or configures a repository on its own: state what
     is missing, and let the user create and push it (or run the commands only
     with their explicit approval, immediately before each one);
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

11. Report the local dashboard URL, GitHub publication readiness, tier-1 and
   tier-2 counts, native role-agent install
   status, distributed-mode readiness when configured, and the next step: run
   Orchestrate.

## Resume semantics

Derive readiness from `graph.json`; do not rely on chat history. A crash loses
only in-flight subagent work. Completed merges, queue entries, verdicts, and
content files remain authoritative.
