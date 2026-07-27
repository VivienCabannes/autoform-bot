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
`PLUGIN_ROOT`, or `CLAUDE_PLUGIN_ROOT`; otherwise use
`Path(<this loaded SKILL.md>).resolve().parents[2]`. The result must be the
directory containing `scripts/`, `skills/`, and `internal/`.

Print the resolved plugin root and target project. Stop if the root does not
contain both `scripts/merge_node.py` and `internal/runbooks/planning.md`.
In every command below, replace `<AUTOFORM_PLUGIN_ROOT>` with that quoted
absolute path; do not depend on a variable exported by a previous shell call.

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

6. Start the dashboard before
   planning so graph changes appear live. Use a free loopback port unless the
   user supplied one. Reuse a server already serving this exact graph.

   ```bash
   nohup uv run --directory "<AUTOFORM_PLUGIN_ROOT>" python -u \
     "<AUTOFORM_PLUGIN_ROOT>/scripts/review_ui/serve_review.py" \
     --graph "$DISPATCH_PROJECT/graph.json" --lean-root "$PROJECT_DIR" \
     --port <PORT_OR_0> >"$DISPATCH_PROJECT/serve_review.log" 2>&1 &
   ```

   Use the requested port or `0` to let the OS choose a free port. Read the
   server's first log line (it reports the actual bound port), verify the
   process is still alive, and report `http://127.0.0.1:<actual-port>/`.

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

8. Read and follow
   `<AUTOFORM_PLUGIN_ROOT>/internal/runbooks/visualization.md` to export and
   build the blueprint. Visualization is part of Setup, not a separate command.

9. Report the dashboard URL, tier-1 and tier-2 counts, native role-agent install
   status, and the next step: run Orchestrate.

## Resume semantics

Derive readiness from `graph.json`; do not rely on chat history. A crash loses
only in-flight subagent work. Completed merges, queue entries, verdicts, and
content files remain authoritative.
