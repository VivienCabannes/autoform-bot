---
description: Stand up an autoform formalization project end-to-end — ensure a Lean 4 + Mathlib project, build the multi-tiered dependency DAG + blueprint from your sources, and launch the local review dashboard ready for /autoform:orchestrate.
argument-hint: "[<project-dir>] [--rebuild] [--port N]   # omit --port → auto-find a free port"
allowed-tools: Read, Write, Edit, Bash, Grep, Glob, Task, Skill
---

# /autoform:setup — build the DAG + blueprint + dashboard

One command from sources to a live, reviewable tiered DAG. Arguments: `$ARGUMENTS`.

## Steps
1. **Lean project.** Target dir = `$ARGUMENTS` path or CWD. If no `lakefile.*`, run the **`make-project`** skill (and **`install-lean`** first if `lake`/`elan` are missing) to create a Lean 4 + Mathlib project. Export `LEAN_PROJECT_DIR` to it. Echo the path.

2. **Dashboard first — so the DAG is visible AS it is built.** Launch the review UI *before* planning, pointed at `<project>/graph.json`, so the user watches nodes appear as the `plan` skill writes each one through `merge_node.py`. Seed an empty shell first if `graph.json` doesn't exist yet, so the server has a file to read. Pick the port — `--port` if given, else **auto-find a free one** (don't hard-code 8765). Detached + idempotent (reuse if one already serves this graph):
   ```
   [ -f <project>/graph.json ] || printf '{"version":2,"metadata":{"sources":[]},"nodes":{}}' > <project>/graph.json
   PORT="${port:-$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); p=s.getsockname()[1]; s.close(); print(p)')}"
   pgrep -f "serve_review.py.*<project>/graph.json" >/dev/null \
     && echo "dashboard already serving this graph" \
     || { nohup python3 ${CLAUDE_PLUGIN_ROOT}/scripts/review_ui/serve_review.py --graph <project>/graph.json --port "$PORT" >> <project>/serve_review.log 2>&1 & echo "started dashboard PID $! on http://127.0.0.1:$PORT/"; }
   ```
   Report the `http://127.0.0.1:$PORT/` URL **now**, and tell the user to reload the graph view to see newly-planned nodes as the build proceeds.

3. **Plan → the multi-tier DAG.** Run the **`plan`** skill unless planning is already COMPLETE. It is INCOMPLETE — so run or RESUME it — when `graph.json` is absent/empty, any tier-1 cluster has no tier-2 children, or any node has `content: null`. `--rebuild` forces a fresh plan from scratch. When you run it:
   - **Confirm sources + scope with the user** (which textbook/paper, in what format — LaTeX/Markdown/PDF — and which chapters/sections). Don't invent prerequisites; ask if the sources don't cover something.
   - Move the source(s) into `sources/`, then build **tier-1** concept clusters (Phase 1) → **tier-2** definitions/statements (Phase 2), producing `graph.json` + one `informal_content/<id>.md` per node. Each merge lands live in the dashboard from step 2.
   - **Resume, don't restart.** `plan` re-derives readiness from the current `graph.json` on entry and writes each node through `merge_node.py` as it lands, so on a PARTIAL graph it continues — splitting only clusters with no tier-2 children and writing only `content: null` prose — instead of redoing finished work. So after a sleep/crash, just re-run `/autoform:setup` and it picks up where it stopped. (Only the subagent work in flight at the moment of the stop is lost; everything already merged is durable on disk.)
   - If planning is already complete (every cluster split, no `content: null`) and `--rebuild` was not passed, keep the graph and say so (skip re-planning).

4. **Blueprint.** Run the **`plan-view`** skill to build the leanblueprint (toolchain check → `export_blueprint.py` → `make web`) so the dashboard can render the typeset statements. The dashboard is already up from step 2; the blueprint just enriches what it renders.

5. **Report**: the dashboard URL (`http://127.0.0.1:<port>/`), the tier-1/2 node counts, and the next step — **`/autoform:orchestrate`** to start reviewing/proving (autonomously, or by dropping agents on nodes in the dashboard, or both).

Keep the human in the loop at the planning gate (sources + scope). Everything after — blueprint, dashboard — is automatic.
