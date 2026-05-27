# Formalization Assessment Pipeline

The eval pipeline assesses whether a Lean 4 formalization codebase faithfully and correctly captures a set of mathematical statements from a textbook. It matches each book statement to a Lean declaration, checks axiom usage, grades quality via an LLM jury, builds a declaration-level dependency graph, and produces a structured report with per-statement scores and aggregate metrics.

Source: `autoform/eval/`

## Pipeline Phases

The pipeline runs in three sequential phases. Phase 1 is a repo-level gate that short-circuits the entire eval on failure. Phase 2 runs per-statement assessment in parallel. Phase 3 aggregates results.

### Phase 1: Repo-Level Gate

Runs once before any per-statement work. Two checks must both pass for the pipeline to proceed.

**Compilation check** (`CompilationChecker` in `lean_checks.py`). Runs `lake build <target>` where the target is derived from the code directory relative to the repo root (e.g., `Atlas.HighDimensionalStatistics`). If the build fails or times out (default 3600s), every statement receives a zero score with the compilation failure message. No further assessment occurs.

**Forbidden keyword check** (`ForbiddenKeywordChecker` in `lean_checks.py`). Scans all `.lean` files under the code directory (excluding `.lake/` and `Unproved.lean`) for metaprogramming keywords: `elab`, `macro`, and `syntax`. These keywords allow modifying the Lean compiler itself, which could bypass proof checking. Comments are stripped before scanning to avoid false positives. Any violation causes all statements to fail.

Both checks feed into `CompilationGrader.create()`, which produces a single `Score` object reused for every statement. The `CompilationGrader` is a binary pass/fail -- its score is either 1.0 or 0.0, and it carries a `compilation` metric of 1 or 0.

### Phase 2: Per-Statement Assessment

If the repo-level gate passes, each book statement is assessed independently, bounded by a concurrency semaphore (default 1000). Two tasks run concurrently: the per-statement matching/grading and the dependency graph build. The per-statement pipeline has three steps.

**Step 1: Matching.** An LLM agent finds the Lean declaration that formalizes the book statement. See [Matching](#matching) below.

**Step 2: Axiom extraction.** The `AxiomsChecker` runs `#print axioms` on the matched declaration to determine its axiom dependencies. The axiom set is attached to the `MatchResult` for use by the grading rubrics. See [Axiom Checking](#axiom-checking) below.

**Step 3: LLM grading.** A jury of LLM judges evaluates the formalization against multiple rubrics concurrently. Each rubric gets its own agent instance. See [LLM Grading](#llm-grading) below.

Results from all three steps are merged into an `EvalResult` per statement. The compilation metrics from Phase 1 are merged with the per-statement jury scores.

### Phase 3: Report

After all statements are assessed, `build_report()` in `metrics.py` aggregates the results into a JSON report. The report is optionally converted to Markdown via `generate_report.py`. A `failed_targets.yaml` file is also produced listing statements that failed or had no match. See [Output Format](#output-format) below.

During Phase 2, progressive report updates are written atomically (via temp file + rename) after each batch of completions, so partial results are always available.

## Matching

**Module:** `matching.py`

**Agent definition:** `agents/matcher/prompt.md` + `agents/matcher/config.yaml`

The matching agent receives a prompt containing the book statement (name, kind, location, description) and paths to the code and book directories. It has access to filesystem tools (`list_directory`, `file_grep`, `read_text_file`, `search_files`) scoped to those directories.

The agent follows a structured search process:
1. Lists the code directory to understand file structure
2. Uses `file_grep` with regex patterns to find declarations (`theorem`, `lemma`, `def`, `abbrev`, `axiom`, `structure`, `class`, `instance`)
3. Reads specific file sections to verify matches
4. Determines the fully qualified declaration name, accounting for `namespace` blocks

The agent returns a JSON response with four fields:
- `lean_declaration` -- the fully qualified Lean name (used directly in `#print axioms`)
- `lean_file` -- relative path to the `.lean` file
- `confidence` -- one of `high`, `medium`, `low`, or `not_found`
- `reasoning` -- explanation of the match

**Axiom validation loop.** After the agent returns a match, the pipeline validates it by running `#print axioms` on the declaration name. If the declaration name cannot be resolved (`DeclarationNotFoundError`), the error message is fed back to the agent, which re-reads the source file and provides a corrected name. This retry loop runs up to 3 times. If all retries fail, the match is kept with `confidence="file_not_built"` to surface the issue rather than marking it as "not covered."

**Response parsing** (`_parse_match_response`). Extracts JSON from the agent's response by finding the last ```` ```json ```` fence. Falls back to searching for a JSON object containing `"lean_declaration"`, then to brace-balanced extraction.

## Axiom Checking

**Module:** `lean_checks.py`, class `AxiomsChecker`

The axiom checker determines which axioms each Lean declaration depends on. It works by creating a temporary `.lean` file in the repo directory containing the necessary imports and `#print axioms <name>` commands, then running it via `lake env lean`.

**Standard axioms** (acceptable in any proof):
- `propext`
- `Classical.choice`
- `Quot.sound`

Any axiom beyond these three is flagged. The most common non-standard axiom is `sorryAx`, which indicates an incomplete proof. Other non-standard axioms include `Lean.ofReduceBool` and `Lean.trustCompiler` (from `native_decide`), or project-defined axioms.

The checker returns two dictionaries keyed by declaration name:
- `all_axioms` -- the complete set of axioms for each declaration
- `violations` -- only the non-standard axioms (those not in `STANDARD_AXIOMS`)

**Error handling.** If a declaration name cannot be resolved (appears as `Unknown constant` in the output or produces no axiom output), the checker raises `DeclarationNotFoundError`. This is a recoverable error -- the matching agent can retry with a corrected name. Fatal errors (timeout, missing `lake` binary) raise `AxiomCheckError`.

## LLM Grading

**Module:** `grading.py`, `compilation_grader.py`

**Agent definition:** `agents/judge/prompt.md` + `agents/judge/config.yaml`

### Rubrics

Rubrics are defined as JSON files in `rubrics/`. Each rubric specifies a `name`, `active` flag, `weight`, `pass_threshold` (minimum score 0-5), `criteria` (per-level descriptions), and a `prompt_template` with placeholders for statement and formalization data (`{name}`, `{description}`, `{lean_declaration}`, `{lean_file}`, `{axioms}`, `{book_dir}`, `{criteria}`, etc.).

The active rubrics and their configurations are:

| Rubric | Weight | Pass Threshold | Focus |
|---|---|---|---|
| `faithfulness` | 0.4 | 4/5 | Statement fidelity to book: quantifiers, hypotheses, domain conditions, scope |
| `proof_integrity` | 0.4 | 3/5 | Proof soundness: axiom justification, sorry usage, structural deception |
| `code_quality` | 0.2 | 3/5 | Mathlib conventions, naming, tactic choice, idiomatic style |

Inactive rubrics (`alignment`, `correctness`, `formatting`, `style`) are present in the rubrics directory but excluded from evaluation (`"active": false`).

### Judge Agent

The judge agent (`agents/judge/prompt.md`) is an expert evaluator with access to three categories of tools:

1. **Dependency graph tools** -- `search_node`, `get_node`, `get_dependency_health`, `list_dependencies`, `list_suspicious_dependencies`, `trace_sorry_dependencies`, `find_dependents`, `overview`
2. **Mathlib search tools** -- `mathlib_grep`, `mathlib_find_name`, `mathlib_read_file`
3. **Filesystem tools** -- `read_text_file`, `file_grep`, `search_files`, `list_directory`

The judge follows a four-step investigation process:
1. Read the book source first (finds the statement in `book.md`, reads the proof if one exists)
2. Read the Lean source code
3. Inspect the dependency graph (calls `get_node` and `get_dependency_health`)
4. Use Mathlib tools if needed to verify API usage

Each judge returns a JSON response with `score` (0-5) and `reasoning`. The `proof_integrity` rubric additionally returns `axiom_only` (boolean) and `axiom_verdicts` (per-axiom justification assessment).

### Jury Aggregation

`LLMJuryGrader` in `compilation_grader.py` implements the aggregation. One `LLMJudgeGrader` is created per rubric, each with its own agent instance. All rubric evaluations run concurrently via `asyncio.gather` (inherited from the parent `JuryGrader` class).

The `aggregate()` method produces the final score:
- **Value:** Weighted average of individual rubric scores (using the rubric weights)
- **Passed:** `True` only if every rubric individually passes its threshold
- **Metrics:** Union of all per-rubric metrics
- **Feedback:** Concatenation of per-rubric feedback lines with score tags (e.g., `[faithfulness=4/5]`)

A statement passes only when all three active rubrics meet their individual thresholds (faithfulness >= 4, proof_integrity >= 3, code_quality >= 3).

## Dependency Graph

**Module:** `dependency_graph/`

The dependency graph is a declaration-level DAG built by running a Lean metaprogram (`lean_script.py`) that introspects the compiled environment. It runs concurrently with per-statement matching during Phase 2.

### Build Process

1. **Lean metaprogram** (`builder.py`, `lean_script.py`). A template Lean script is written to a temporary file and executed via `lake env lean`. The script iterates over all constants in the environment whose module name starts with the given prefix, and for each declaration emits a pipe-delimited line:
   ```
   NAME|KIND|IS_CLASS|TYPE_HEAD|HAS_SORRY|BODY_TAGS|DEP1,DEP2,...|FIELD_DEP1,...|IS_UNPROVED
   ```
   The script performs expression-level analysis to detect body-level tags: `vacuous_body`, `ignores_params`, `proof_by_exfalso`, `proof_by_subsingleton`, `returns_assumption`, `field_projection_body`, `custom_hypothesis_in_type`, `trivial_constructor`.

2. **Graph-level tagging** (`tagger.py`). Adds cross-referencing tags that require knowledge of the full graph:
   - `orphan_class` -- a class with zero project instances
   - `trivial_instance` -- an instance whose body has a suspicious tag (e.g., `vacuous_body`, `proof_by_exfalso`)

3. **Transitive closure** (`__init__.py`). Computes `all_deps` (transitive dependency closure), `transitive_axioms` (project axiom names reachable transitively, plus `sorryAx` if any node in the chain uses sorry), and `cone_alerts` (precomputed alerts derived from transitive dependencies).

### GraphNode Fields

Each node in the graph (`types.py`) carries: `name`, `kind` (theorem/def/axiom/inductive/constructor/recursor/opaque/quot), boolean flags (`is_class`, `is_auto_generated`, `has_sorry`, `is_unproved`), `instance_count`, `type_head`, `deps`/`field_deps` (direct dependencies), `tags` (structural flags), and precomputed transitive fields (`all_deps`, `transitive_axioms`, `cone_alerts`).

### Support Cone

`support_cone()` in `cone.py` extracts the transitive support cone for a target declaration -- all nodes reachable via dependencies. It derives target-level alerts such as `depends_on_vacuous_definition`, `depends_on_orphan_class_field`, `depends_on_sorry_definition`, `has_unproved_dependencies`, etc. The cone also produces a human-readable summary with statistics for the judge agent.

### Unproved Detection

The builder (`builder.py`) detects declarations marked with `@[unproved]` (a project-defined attribute indicating the book does not provide a proof) via two mechanisms:
1. The Lean metaprogram checks `unprovedAttr.hasTag` when the `Unproved` module is imported
2. A Python-side fallback scans source files for `unproved <name>` and `@[unproved] axiom <name>` patterns

### Inherited Failure Analysis

After all assessments complete, `_annotate_inherited_failures()` in `pipeline.py` checks whether a failed statement's unjustified axioms are inherited from transitive dependencies that are themselves evaluated statements with the same axiom. If all unjustified axioms are inherited (the root cause is upstream), the failure is marked `inherited_failure=True` and the statement is flipped to passed. Summary counts are then recomputed.

## Merge Gating

**Module:** `utils/gate.py`, class `EvalGate`

`EvalGate` wraps the eval machinery for two use cases:
1. **Merge gate** -- evaluate a subset of statements against a worker's worktree during the autoformalization pipeline
2. **Full eval** -- run the complete evaluation

It is used as an async context manager. On entry, it runs repo-level checks (forbidden keywords, axiom extraction) and creates one judge agent per rubric. On exit, it cleans up all agents.

The grading pipeline inside `EvalGate` uses `StatementGrader`, which composes three graders in a short-circuit chain:
1. `CompilationGrader` -- binary pass/fail (same for all statements)
2. `AxiomGrader` -- per-declaration axiom check from precomputed violations
3. `LLMJuryGrader` -- weighted rubric aggregation

If compilation or axioms fail, LLM rubrics are skipped entirely.

Usage pattern:
```python
async with EvalGate(make_inference, targets, repo_dir=repo) as gate:
    result = await gate.evaluate_statements([1, 3, 5], repo)
    if not result.passed:
        print(result.feedback)
```

`EvalGateResult` contains:
- `passed` -- `True` only if all evaluated statements pass
- `statement_scores` -- per-statement `Score` objects keyed by statement ID
- `feedback` -- human-readable multi-line summary

Precomputed repo-level results can be injected via `RepoCheckResults` to avoid redundant work when the caller has already run these checks.

## Configuration and Usage

### CLI

```bash
python -m autoform.eval run \
    --repo_dir=/path/to/lean/repo \
    --code_dir=/path/to/lean/source \
    --task_file=/path/to/statements.yaml \
    --book_dir=/path/to/book/source \
    --model="Opus 4.6" \
    --concurrency=1000 \
    --skip_compilation \
    --report_path=/path/to/report.json
```

**Required arguments:**
- `repo_dir` -- path to the Lean repository root (where `lakefile.toml` lives)
- `code_dir` -- path to the Lean source directory to evaluate (e.g., `Atlas/HighDimensionalStatistics`)
- `task_file` -- path to the YAML task list
- `book_dir` -- path to the book source directory

**Optional arguments:**
- `model` -- LLM model for matching and judging (default: `"Opus 4.6"`)
- `concurrency` -- max concurrent per-statement assessment tasks (default: 1000)
- `skip_compilation` -- skip the compilation gate (useful when the build is known broken but you want to test matching/grading)
- `report_path` -- path for progressive `report.json` updates and `dependency_graph.json`

### Task File Format

The task file is a YAML list of `FormalizationTarget` entries:
```yaml
- name: "Theorem 1.17"
  description: "If X is a sub-Gaussian random variable..."
  kind: "theorem"
  location: "Chapter 1, Section 1.3"
```

Each entry has:
- `name` (required) -- identifier for the statement
- `description` -- mathematical content in natural language or LaTeX
- `kind` -- statement type (theorem, lemma, definition, proposition, corollary)
- `location` -- where to find it in the book

### Output Files

The pipeline writes to a run directory under `autoform/eval/output/traces/<run_id>/`:
- `report.json` -- the full structured report
- `report.md` -- markdown rendering of the report
- `cost.json` -- token usage and cost summary
- `dependency_graph.json` -- the full dependency graph (if `report_path` was set)
- `failed_targets.yaml` -- task entries for statements that failed or were not covered
- Individual trace files for each agent invocation (matcher, judge)

## Output Format

### JSON Report Structure

```json
{
  "repo": {
    "compiles": true,
    "compilation_output": "...",
    "forbidden_keyword_violations": [],
    "all_checks_passed": true
  },
  "statements": {
    "summary": {
      "total": 42, "passed": 35, "failed": 7, "pass_rate": 0.833,
      "compilation": 1.0, "faithfulness": 3.85,
      "proof_integrity": 3.42, "code_quality": 3.71
    },
    "details": [
      {
        "id": "theorem_1_17", "passed": true,
        "scores": { "compilation": 1, "faithfulness": 4, "proof_integrity": 5, "code_quality": 4 },
        "feedback": "[faithfulness=4/5] ...",
        "idx": 0, "name": "Theorem 1.17", "kind": "theorem",
        "lean_declaration": "theorem_1_17", "lean_file": "Atlas/Book/Chapter1.lean",
        "match_confidence": "high", "axioms": "propext, Classical.choice",
        "deps": { "direct": ["..."], "transitive": ["..."] },
        "inherited_failure": null, "axiom_verdicts": {}, "axiom_only": false
      }
    ]
  },
  "progress": { "completed": 42, "total": 42 },
  "dependency_graph_size": 256
}
```

### Markdown Report

The markdown report (`generate_report.py`) organizes statements into three sections:

1. **Issues** -- matched declarations that failed evaluation, with per-rubric feedback for failing rubrics (score < 3). These are the actionable items.
2. **Not Covered** -- statements with no matching declaration found, listed as a table.
3. **Passed** -- statements that passed all rubrics, shown in a summary table with scores.

The report header shows aggregate statistics (total, passed, issues, not covered, pass rate) and the per-rubric averages.
