# Autoform Visualizer

## Overview

The Autoform Visualizer is a web dashboard for inspecting and monitoring autoformalization pipeline runs. It provides real-time and post-hoc views of DAG task execution, agent traces, evaluation reports, dependency graphs, cost/token usage, and live agent interaction. The visualizer supports both single-run mode (one run directory) and hub mode (multiple runs behind a gateway).

## Running

### Single-run mode

Start the visualizer pointing at a directory that contains one or more run subdirectories:

```bash
python -m autoform.visualizer.app --runs-dir /path/to/runs --port 8001
```

Each run subdirectory is expected to have the structure:

```
{run}/
    dag.json
    traces/
        orchestrator.json
        tasks/{task_id}/
            attempt_1/
                steps.json
                worker-0.json
                reviewer-0.json
            attempt_2/
                ...
            analyzer.json
```

The visualizer auto-discovers all run subdirectories that contain either a `dag.json` file or a `traces/` directory.

### Hub mode

Hub mode spawns an isolated visualizer subprocess per run and proxies all requests through a single port. This is useful for monitoring many runs simultaneously without cross-contamination of state or caches.

```bash
python -m autoform.visualizer.hub --runs-dir /path/to/runs --port 8001
```

On startup, the hub discovers all run directories and pre-spawns a per-run visualizer server on an ephemeral port. The hub gateway at the configured port serves the runs listing page directly and proxies all run-specific requests (including API calls and SSE streams) to the appropriate child process. If a child process dies, it is automatically respawned on the next request.

### Standalone trace viewer

Generate a self-contained HTML file from a single agent trace JSON:

```bash
python -m autoform.visualizer.trace_viewer path/to/agent_trace.json
# -> path/to/agent_trace.html

# Or serve it directly on a local port:
python -m autoform.visualizer.trace_viewer path/to/agent_trace.json --serve 8000
```

## Views

### Runs list (`/`)

Lists all discovered run directories as clickable cards. In hub mode, each card links to `/run/{run_name}` which proxies to that run's isolated server. In single-run mode (when the `VIZV1_RUN_FILTER` env var is set), only the filtered run is shown.

### DAG view (`/run/{run_name}`)

The main run page. Shows:

- **Escalation banner** -- a prominent alert linking to the escalations page when active (non-dismissed) escalations exist, color-coded by severity (red for critical, amber for warnings).
- **Run header** -- run name, total duration, link to the orchestrator trace, and a shutdown button.
- **DAG diagram** -- a collapsible Mermaid flowchart of tasks and their dependency edges, loaded on demand. Nodes are color-coded by status (green=completed, red=failed, yellow=in-progress, slate=pending, gray=removed). Clickable nodes navigate to the task detail page. The diagram can be saved as SVG.
- **Task list** -- a sortable, filterable table of all tasks from `dag.json`. Supports status filter chips (all, completed, failed, in-progress, pending, deleted), text search, and column sorting by attempt count or cost. Per-task cost and attempt data are populated from `stats.json` when available.

### Task detail (`/run/{run_name}/traces/task/{task_id}`)

Shows the full description of a single DAG task along with a table of all attempts. Each attempt row displays its status (success/failed), the winning agent ID, number of agents involved, wall-clock duration, token count, and cost. Links to the trace analyzer if one exists. Clicking an attempt row navigates to the attempt detail page.

### Attempt detail (`/run/{run_name}/traces/task/{task_id}/attempt/{n}`)

Shows a single attempt in detail:

- **Header** -- attempt number, task ID, total cost, total tokens, and status badge.
- **Winner indicator** -- highlighted when a winning agent was selected.
- **Task description** -- the full task prompt (collapsible).
- **Agents table** -- per-agent breakdown showing status, turn count, tokens, cost, and a link to the full agent trace (rendered via the trace viewer).
- **Step timeline** -- the orchestration step log from `steps.json`, showing each step function (review, build, rebase, merge, etc.) with its duration, success/failure status, and expandable result details.

### Goals (`/run/{run_name}/goals`)

Displays formalization goals from `goals.json` with:

- **Summary cards** -- total, completed, failed, pending counts, and overall pass rate percentage.
- **Progress bar** -- visual bar showing completed (green) and failed (red) proportions.
- **Filter tabs** -- filter by all, completed, failed (with sub-filters for failure reasons: axioms, faithfulness, compilation), and pending.
- **Goals list** -- expandable cards for each goal showing status badge, title, kind, metric scores (faithfulness, proof integrity, code quality, axiom checks), Lean declaration name, file path, match confidence, and detailed feedback.

### Usage (`/run/{run_name}/usage`)

Token and cost analytics:

- **Cost by category** -- cards breaking down cost across workers, reviewers, orchestrator, analyzers, readers, full eval, and supervisor (merge eval). Uncategorized trace files are shown in an expandable detail section.
- **Token usage by model** -- per-model breakdowns showing total input/output tokens, cache read/write/hit rates, and cost. Layout adapts to the provider (Anthropic shows cache creation tokens; OpenAI/Gemini show cached vs uncached input).
- **Cost by task** -- table sorted by descending cost, showing attempt count per task.
- **Generate/Update Stats** button -- triggers background computation that walks all trace JSON files, aggregates costs, and writes `stats.json`. A progress bar tracks file processing.

### Insights (`/run/{run_name}/insights`)

Analytics and historical data:

- **Progress chart** -- interactive Chart.js plot with configurable axes. Y-axis options: goals completed (count or percentage), tasks completed, lines of code (total and code-only), rounds per attempt (bar chart). X-axis options: cumulative tokens, cumulative cost, active runtime, wall-clock runtime. Data is built from traces, steps, goal events, and git history. Supports build, fresh rebuild, and export to JSON.
- **Lines of Code** -- current total and code-only (excluding comments/blanks) Lean line counts.
- **Task outcomes by attempt count** -- bar chart showing how many tasks completed vs. were deleted, grouped by how many attempts they needed.
- **Cost per task by outcome** -- bar chart comparing average and median cost for completed vs. deleted tasks.
- **Git commit history** -- list of recent commits with checkboxes to select any two for a diff comparison.
- **Skills** -- expandable list of agent-written skill files (markdown documents).
- **Reports** -- expandable list of task report JSON files.

### Live monitoring (`/run/{run_name}/live`)

The agents lobby, which shows all currently registered agents fetched from the pipeline's agent registry. Displays:

- **Node status badges** -- per-node online/unreachable indicators.
- **Search and filter bar** -- text search across agent name, node, and type. Dropdown filters for status (running/idle/pending) and type (worker/reviewer/orchestrator/analyzer).
- **Agent table** -- rows showing status (with animated pulse for running agents), agent ID, type, node, turn count, and pending message count. Clicking a row navigates to the live chat view.

### Live chat (`/run/{run_name}/live/{agent_id}`)

Real-time conversation view for a single agent:

- **Conversation pane** -- renders the agent's message history (user, assistant, tool messages) with role-colored cards. Truncates very long messages. Shows pending/queued messages in amber. Auto-polls every 2 seconds.
- **Chat input** -- text field and send button to inject a human message into the agent. Messages can be sent directly (if the agent is idle) or queued (if it is busy).

### Eval (`/run/{run_name}/eval`)

Evaluation management:

- **Trigger evaluation** -- button to launch an eval subprocess against the current code, with options for local or SLURM mode and configurable concurrency.
- **Live progress panel** -- during evaluation, shows a progress bar, count of assessed/passed/failed targets, and current pass rate. Polls every 3 seconds.
- **Stop eval** -- terminates a running eval process.
- **Latest report** -- when complete, shows commit hash, compilation status, summary cards (total, passed, issues, not covered, pass rate), a progress bar, and expandable per-target details with scores and feedback.
- **Transfer to Goals** -- copies the latest eval results into `goals.json` for the Goals view.
- **History** -- collapsible list of previous eval reports.

### Dependency graph (`/run/{run_name}/depgraph`)

Deep structural analysis of the Lean codebase built from declaration-level dependencies:

- **Overview cards** -- total declarations, fully proved count, direct/transitive sorry counts, custom axioms, orphan classes, declarations with alerts, unproved declarations.
- **Proof integrity panel** -- progress bar for fully proved ratio, axiom pollution rankings.
- **Structural red flags** -- counts of each flag type (vacuous body, ignores params, proof by exfalso, proof by subsingleton, returns assumption, field projection body, trivial constructor, orphan class, trivial instance) with descriptions.
- **Dependency shape** -- kind distribution, root/leaf counts, median cone size, largest cones, most-depended-on declarations, bottleneck nodes.
- **Class and typeclass analysis** -- project classes with instance counts, orphan class detection.
- **Quality signals** -- clean theorem ratio, axiom-to-theorem ratio, sorry distribution by namespace.
- **Namespace heatmap** -- table of namespaces showing sorry, axiom, flagged, orphan-dep, and clean counts with color-coded health percentage.
- **Axiom blast radius** -- for each project axiom, shows how many declarations depend on it and how many would become fully proved if it were resolved.
- **Declaration list** -- searchable, filterable list with kind/view/flag filters. Clicking a declaration opens the inspector panel showing deps, fan-in, transitive axioms, and tags.
- **Support cone modal** -- visualizes the full transitive dependency tree of a declaration as both a Mermaid graph and an interactive tree. Shows cone alerts, summary statistics, and per-node detail on click.

### Compare (`/run/{run_name}/compare/{base}..{head}`)

Side-by-side diff view between two git commits in the run's code directory. Shows a sticky file tree sidebar, per-file diffs with syntax-highlighted additions/deletions, hunk headers, and GitHub-style change bars. Files can be individually collapsed.

### Commit (`/run/{run_name}/commit/{sha}`)

Detailed view of a single git commit. Shows commit metadata (author, date, full SHA), file summary with addition/deletion counts, and per-file unified diffs with the same layout as the compare view.

### Escalations (`/run/{run_name}/escalations`)

Displays pipeline escalations (issues raised by agents or the orchestrator) from `escalations.jsonl`:

- **Filters** -- by status (active/dismissed/all), severity (critical/warning/decomposition), and source (orchestrator/workers).
- **Escalation cards** -- color-coded by severity (red for critical, blue for decomposition, amber for warning). Each shows the message, agent ID, timestamp, and a dismiss button.
- **Reply capability** -- for active escalations, an inline reply form lets you send a message to the escalating agent or the orchestrator, injecting it into their conversation.
- **Bulk dismiss** -- dismiss all currently visible escalations at once.

### Merge batches (`/run/{run_name}/merge-batches`)

History of merge queue operations where agent work is landed into the codebase:

- **Batch cards** -- each shows pre/post commit hashes, batch status (evaluated, landed, bisected, rejected), associated task ID, and total cost.
- **Eval summary** -- for evaluated batches, shows passed/failed/total counts with a progress bar.
- **Merge queue steps** -- collapsible timeline of queue operations (land, evaluate, bisect, etc.) with duration and success/failure status.
- **Eval agent traces** -- links to the traces of eval sub-agents (matcher, judges, triage).
- **Target details** -- per-statement results with faithfulness, proof integrity, and code quality scores.

### Hardware (`/run/{run_name}/hardware`)

Per-node hardware monitoring fetched from the pipeline's control plane:

- **Summary bar** -- total nodes (online/total), allocated CPUs, memory usage, Lean process count.
- **Per-node cards** -- each shows hostname, rank, uptime, CPU usage bar (relative to allocation), memory usage bar, and a table of Lean processes sorted by RSS with PID and CPU percentage.

## API Endpoints

### Stats and data generation

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/run/{run_name}/stats` | Triggers background aggregation of all trace files into `stats.json`. Returns immediately. |
| `GET` | `/api/run/{run_name}/stats/status` | Polls whether stats generation is complete, with file progress. |
| `POST` | `/api/run/{run_name}/progress-data` | Builds progress chart data (usage timeline, goals, tasks, LOC, rounds) in background. Supports `?fresh=true` to rebuild from scratch. |
| `GET` | `/api/run/{run_name}/progress-data/status` | Polls progress data build status. |
| `GET` | `/api/run/{run_name}/progress-data` | Returns cached progress data from disk. |
| `POST` | `/api/run/{run_name}/export-plot-data` | Exports all plot data combinations as JSON to `~/ablations/`. |

### Evaluation

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/eval?run={name}` | Launches an eval subprocess against the run's current code. Supports `&mode=local\|slurm` and `&concurrency=N`. |
| `POST` | `/api/eval/stop?run={name}` | Terminates a running eval process by PID. |
| `GET` | `/api/eval-progress?run={name}` | Returns live eval progress (completed/total counts and partial report). |
| `POST` | `/api/eval/transfer-to-goals?run={name}` | Transfers the latest eval report results into `goals.json`. |

### Pipeline control

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/shutdown?run={name}` | Proxies a shutdown request to the pipeline's control plane, stopping all in-flight tasks. |

### Dependency graph

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/run/{run_name}/depgraph/build` | Triggers background construction of the dependency graph. |
| `GET` | `/api/run/{run_name}/depgraph/data` | Returns the full dependency graph as JSON (all declarations with deps, tags, alerts). |

### Live agents

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/agent/{agent_id}/messages?run={name}` | Returns the agent's current conversation history and pending messages. |
| `POST` | `/api/agent/{agent_id}/message?run={name}` | Sends a human message to the agent (delivered directly or queued). |

### Escalations

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/run/{run_name}/escalation/{index}/dismiss` | Marks an escalation as dismissed. |
| `GET` | `/api/run/{run_name}/escalations/count` | Returns count of active escalations by severity. |

### DAG

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/run/{run_name}/dag` | Returns the Mermaid diagram definition for the task DAG. |

## Trace Viewer

The standalone trace viewer (`autoform/visualizer/trace_viewer.py`) generates a fully self-contained HTML file from an `AgentTrace` JSON. The output requires no external dependencies -- all CSS and JavaScript are inlined.

The generated HTML includes five sections:

1. **Header** -- agent ID, status badge, trace metadata (start time, duration, model).
2. **Stats cards** -- duration, turns, LLM call count, tool call success rate, total tokens (in/out breakdown), and cost.
3. **LLM calls table** -- per-call detail with model, relative time, latency, input/output token counts, and cost. Includes a totals footer row.
4. **Tool use** -- aggregate stats per tool (count, success/fail rate, min/avg/max/total duration) and an individual calls table with collapsible arguments and results.
5. **Time breakdown** -- visual bar showing LLM time, tool time, and overhead as proportions of total duration.
6. **Conversation** -- full message history with role-colored blocks (system, user, assistant). Assistant messages show collapsible thinking blocks, inline tool call cards with arguments/results, and status badges.

When served through the main visualizer (via the `/run/{run_name}/agent-trace/` route), the trace HTML is augmented with an auto-refresh script that polls for updates every 10 seconds.
