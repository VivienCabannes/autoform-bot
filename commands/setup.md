---
description: Stand up an autoform formalization project end-to-end — ensure a Lean 4 + Mathlib project, build the multi-tiered dependency DAG + blueprint from your sources, and launch the local review dashboard ready for /autoform:orchestrate.
argument-hint: "[<project-dir>] [--rebuild] [--port 8765]"
allowed-tools: Read, Write, Edit, Bash, Grep, Glob, Task, Skill
---

# /autoform:setup — build the DAG + blueprint + dashboard

One command from sources to a live, reviewable tiered DAG. Arguments: `$ARGUMENTS`.

## Steps
1. **Lean project.** Target dir = `$ARGUMENTS` path or CWD. If no `lakefile.*`, run the **`setup-project`** skill (and **`install-lean`** first if `lake`/`elan` are missing) to create a Lean 4 + Mathlib project. Export `LEAN_PROJECT_DIR` to it. Echo the path.

2. **Plan → the multi-tier DAG.** If `graph.json` is absent (or `--rebuild` is passed), run the **`plan`** skill:
   - **Confirm sources + scope with the user** (which textbook/paper, in what format — LaTeX/Markdown/PDF — and which chapters/sections). Don't invent prerequisites; ask if the sources don't cover something.
   - Move the source(s) into `sources/`, then build **tier-1** concept clusters (Phase 1) → **tier-2** definitions/statements (Phase 2), producing `graph.json` + one `informal_content/<id>.md` per node.
   - If `graph.json` already exists and `--rebuild` was not passed, keep it and say so (skip re-planning).

3. **Blueprint.** Run the **`plan-view`** skill to build the leanblueprint (toolchain check → `export_blueprint.py` → `make web`) so the dashboard can render the typeset statements.

4. **Dashboard.** Launch the review UI on `127.0.0.1`, detached (idempotent — if one already serves this graph, reuse it):
   ```
   pgrep -f "serve_review.py.*<project>/graph.json" >/dev/null \
     && echo "dashboard already serving this graph" \
     || { nohup python3 ${CLAUDE_PLUGIN_ROOT}/scripts/review_ui/serve_review.py --graph <project>/graph.json --port ${port:-8765} >> <project>/serve_review.log 2>&1 & echo "started dashboard PID $!"; }
   ```

5. **Report**: the dashboard URL (`http://127.0.0.1:<port>/`), the tier-1/2 node counts, and the next step — **`/autoform:orchestrate`** to start reviewing/proving (autonomously, or by dropping agents on nodes in the dashboard, or both).

Keep the human in the loop at the planning gate (sources + scope). Everything after — blueprint, dashboard — is automatic.
