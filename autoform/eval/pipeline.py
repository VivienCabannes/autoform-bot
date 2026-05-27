# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Per-statement assessment — matching, axiom check, and grading."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import re
import tempfile
from collections.abc import Callable
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from core.agent import Agent
from core.agent.loader import load_agent_definition
from core.eval.dtypes import EvalResult, Score
from core.inference import InferenceProtocol
from core.mcp import MCPServerConfig
from core.trace import AgentTrace, TraceStore
from autoform.eval.lean_checks import (
    AxiomsChecker,
    CompilationChecker,
    ForbiddenKeywordChecker,
)
from autoform.eval.metrics import build_report
from autoform.eval.compilation_grader import CompilationGrader
from autoform.eval.types import FormalizationTarget
from tools.files.filesystem.server import FilesystemConfig, filesystem_server
from tools.search.mathlib.server import MathlibConfig, mathlib_server

from .dependency_graph import DependencyGraph, build_dependency_graph
from .grading import grade_statement, rubric_count
from .matching import match_statement
from .tools.dep_graph.server import dep_graph_server
from .types import AssessmentTarget

logger = logging.getLogger(__name__)

_JUDGE_AGENT_DIR = Path(__file__).resolve().parent / "agents" / "judge"

_LEAN_DECL_RE = re.compile(
    r"^(?:@\[.*?\]\s*)*(?:private\s+|protected\s+|noncomputable\s+|unsafe\s+)*"
    r"(?:theorem|lemma|def|definition|instance|abbrev|structure|class|inductive|axiom)\s",
    re.MULTILINE,
)
_MAX_LEAN_SOURCE_CHARS = 3000


def _extract_lean_source(file_path: Path, decl_name: str) -> str | None:
    """Extract a Lean declaration's source from a file.

    Finds the declaration by name and captures from the declaration keyword
    through to the next top-level declaration or end of file.
    """
    try:
        content = file_path.read_text(errors="replace")
    except OSError:
        return None

    for m in _LEAN_DECL_RE.finditer(content):
        line_end = content.find("\n", m.start())
        line = content[m.start() : line_end] if line_end != -1 else content[m.start() :]
        if decl_name not in line:
            continue

        next_match = _LEAN_DECL_RE.search(content, line_end + 1 if line_end != -1 else m.end())
        end = next_match.start() if next_match else len(content)
        source = content[m.start() : end].rstrip()
        if len(source) > _MAX_LEAN_SOURCE_CHARS:
            source = source[:_MAX_LEAN_SOURCE_CHARS] + "\n-- [truncated]"
        return source

    return None


def _atomic_write(path: Path, content: str) -> None:
    """Write content to path atomically via temp file + rename."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        os.write(fd, content.encode())
        os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


async def assess_statement(
    idx: int,
    statement: FormalizationTarget,
    code_dir: Path,
    repo_dir: Path,
    book_dir: Path,
    compilation: CompilationGrader,
    axiom_checker: AxiomsChecker,
    mathlib_cfg: MCPServerConfig,
    inference_factory: Callable[[], InferenceProtocol],
    stack: AsyncExitStack,
    trace_store: TraceStore | None = None,
    dep_graph: asyncio.Task[DependencyGraph | None] | DependencyGraph | None = None,
) -> EvalResult:
    """Run the full assessment pipeline for a single book statement.

    1. Match the book statement to a Lean declaration.
    2. Extract axioms used by the declaration.
    3. Grade via LLM jury (faithfulness, axiom justification).
    """
    judge_definition = load_agent_definition(_JUDGE_AGENT_DIR)

    # Step 1: Match (with axiom validation)
    logger.info("Matching: %s", statement.name)
    match = await match_statement(
        statement,
        code_dir,
        book_dir,
        inference_factory,
        trace_store,
        idx=idx,
        axiom_checker=axiom_checker,
    )

    if match.confidence == "not_found" or match.lean_declaration is None:
        return EvalResult(
            datum_id=statement.name,
            score=Score(
                value=0.0,
                passed=False,
                feedback=f"No match found: {match.reasoning}",
                metrics={"compilation": 1},
            ),
            datum=AssessmentTarget(
                idx=idx,
                name=statement.name,
                description=statement.description,
                kind=statement.kind,
                location=statement.location,
                book_dir=str(book_dir),
                match_confidence=match.confidence,
                match_reasoning=match.reasoning,
            ),
        )

    if match.confidence == "file_not_built":
        return EvalResult(
            datum_id=statement.name,
            score=Score(
                value=0.0,
                passed=False,
                feedback=f"File not built: {match.reasoning}",
                metrics={"compilation": 0, "faithfulness": 0, "proof_integrity": 0},
            ),
            datum=AssessmentTarget(
                idx=idx,
                name=statement.name,
                description=statement.description,
                kind=statement.kind,
                location=statement.location,
                lean_declaration=match.lean_declaration,
                lean_file=match.lean_file,
                book_dir=str(book_dir),
                match_confidence=match.confidence,
                match_reasoning=match.reasoning,
            ),
        )

    # Resolve dep_graph future (graph build runs concurrently with matching)
    if isinstance(dep_graph, asyncio.Task):
        dep_graph = await dep_graph

    # Step 2: Build axiom string from match result (already validated in matching)
    axiom_set = match.axioms
    axioms_str = ", ".join(sorted(axiom_set)) if axiom_set else "No axiom dependencies"

    # Extract Lean source code for the matched declaration
    lean_source = None
    if match.lean_file and match.lean_declaration:
        lean_source = _extract_lean_source(code_dir / match.lean_file, match.lean_declaration)

    # Look up declaration dependencies from the pre-computed graph
    deps_str = ""
    if dep_graph and match.lean_declaration:
        node = dep_graph.get(match.lean_declaration)
        if node and node.deps:
            deps_str = ", ".join(node.deps)

    # Build AssessmentTarget
    # Escape { and } in description to prevent format_map issues in rubric templates
    safe_description = statement.description.replace("{", "{{").replace("}", "}}")
    target = AssessmentTarget(
        idx=idx,
        name=statement.name,
        description=safe_description,
        kind=statement.kind,
        location=statement.location,
        lean_declaration=match.lean_declaration,
        lean_file=match.lean_file,
        lean_source=lean_source,
        axioms=axioms_str,
        deps=deps_str,
        book_dir=str(book_dir),
        match_confidence=match.confidence,
        match_reasoning=match.reasoning,
    )

    # Step 3: LLM judges (one agent per rubric for concurrent evaluation)
    logger.info("Grading: %s", statement.name)

    persist_dir = trace_store.run_dir if trace_store else None
    extra_read = (str(persist_dir),) if persist_dir else ()
    fs_cfg = filesystem_server(
        FilesystemConfig(
            allowed_dirs=(str(code_dir), str(book_dir)),
            extra_read_dirs=extra_read,
        )
    )
    server_configs: list[MCPServerConfig] = [mathlib_cfg, fs_cfg]

    # Dynamically add dep_graph server and tools when graph is available
    dep_graph_tools = [
        "search_node",
        "get_node",
        "get_dependency_health",
        "list_dependencies",
        "list_suspicious_dependencies",
        "trace_sorry_dependencies",
        "find_dependents",
        "overview",
    ]
    if dep_graph:
        server_configs.append(dep_graph_server(dep_graph))
        # Extend allowlist so the agent can use graph tools
        if judge_definition.tool_allowlist:
            judge_definition = dataclasses.replace(
                judge_definition,
                tool_allowlist=judge_definition.tool_allowlist + dep_graph_tools,
            )

    num_rubrics = rubric_count()
    agents: list[Agent] = []
    traces: list[AgentTrace] = []
    for i in range(num_rubrics):
        judge_id = f"judge/target_{idx}/{i}"
        agent = await stack.enter_async_context(
            Agent(
                definition=judge_definition,
                inference=inference_factory(),
                server_configs=server_configs,
                trace_store=trace_store,
                id=judge_id,
                persist_dir=persist_dir,
            )
        )
        trace = AgentTrace(id=judge_id)
        agent.set_trace(trace)
        agents.append(agent)
        traces.append(trace)
    score = await grade_statement(target, agents)
    for agent in agents:
        agent.finalize_trace()
    if trace_store:
        for trace in traces:
            trace_store.save(trace)

    merged_metrics = {**compilation.score.metrics, **score.metrics}
    return EvalResult(
        datum_id=match.lean_declaration,
        score=Score(
            value=score.value,
            passed=score.passed,
            feedback=score.feedback,
            metrics=merged_metrics,
        ),
        datum=target,
    )


# ---------------------------------------------------------------------------
# Batch assessment
# ---------------------------------------------------------------------------

_DEFAULT_METRIC_KEYS = ("compilation", "faithfulness", "proof_integrity", "code_quality")
_DEFAULT_DETAIL_FIELDS = (
    "idx",
    "name",
    "description",
    "kind",
    "location",
    "lean_declaration",
    "lean_file",
    "lean_source",
    "match_confidence",
    "axioms",
    "deps",
)


async def assess_targets(
    targets: list[FormalizationTarget],
    code_dir: Path,
    repo_dir: Path,
    book_dir: Path,
    mathlib_path: Path,
    inference_factory: Callable[[], InferenceProtocol],
    *,
    indices: list[int] | None = None,
    concurrency: int = 100_000,
    skip_compilation: bool = False,
    trace_store: TraceStore | None = None,
    metric_keys: tuple[str, ...] = _DEFAULT_METRIC_KEYS,
    detail_fields: tuple[str, ...] = _DEFAULT_DETAIL_FIELDS,
    report_path: Path | None = None,
    progress_batch: int = 1,
) -> tuple[dict[str, Any], list[EvalResult]]:
    """Compile, assess a list of targets, and build the report.

    Args:
        indices: Original target indices. When assessing a subset,
            pass the original indices so reports show correct numbering.
            Defaults to 0..len(targets).
        report_path: If provided, write progressive report.json every
            ``progress_batch`` completions.
        progress_batch: How often to flush the progressive report
            (every N completed statements).

    Returns the report dict and the list of per-target EvalResults
    (for callers that need to inspect individual outcomes).
    """
    target_indices = indices if indices is not None else list(range(len(targets)))
    total = len(targets)

    # Repo-level checks
    if skip_compilation:
        compiled, compilation_output = True, "Compilation skipped"
        forbidden_violations: list[tuple[str, str]] = []
    else:
        build_target = str(code_dir.relative_to(repo_dir)).replace("/", ".")
        logger.info("Running compilation check: lake build %s ...", build_target)
        compiled, compilation_output = await CompilationChecker(repo_dir, target=build_target).check()
        if compiled:
            logger.info("Compilation succeeded")
        else:
            logger.error("Compilation FAILED:\n%s", compilation_output[-500:])
        forbidden_violations = ForbiddenKeywordChecker(code_dir).check()

    compilation = CompilationGrader.create(compiled, compilation_output, forbidden_violations)

    if not compilation.score.passed:
        logger.warning("Compilation failed — all targets receive failing score: %s", compilation.score.feedback)
        results = [
            EvalResult(
                datum_id=s.name,
                score=compilation.score,
                datum=AssessmentTarget(idx=target_indices[i], name=s.name, description=s.description),
            )
            for i, s in enumerate(targets)
        ]
        report = build_report(
            compiles=compiled,
            compilation_output=compilation_output,
            forbidden_keyword_violations=forbidden_violations,
            results=results,
            metric_keys=metric_keys,
            detail_fields=detail_fields,
        )
        report["progress"] = {"completed": total, "total": total}
        if report_path:
            _atomic_write(report_path, json.dumps(report, indent=2))
        return report, results

    # Per-target assessment
    mathlib_cfg = mathlib_server(MathlibConfig(repo_root=str(mathlib_path)))
    axiom_checker = AxiomsChecker(repo_dir)
    sem = asyncio.Semaphore(concurrency)

    # Declaration-level dependency graph (runs concurrently with matching)
    module_prefix = str(code_dir.relative_to(repo_dir)).replace("/", ".").replace("\\", ".")

    async def _build_graph() -> DependencyGraph | None:
        try:
            logger.info("Building dependency graph for module prefix: %s", module_prefix)
            graph = await build_dependency_graph(
                repo_dir=repo_dir,
                module_prefix=module_prefix,
                import_module=module_prefix,
            )
            logger.info("Dependency graph complete: %d declarations", graph.size)
            if report_path:
                graph.save(report_path.parent / "dependency_graph.json")
            return graph
        except Exception as e:
            logger.warning("Dependency graph build failed (non-fatal): %s", e)
            return None

    dep_graph_task: asyncio.Task[DependencyGraph | None] = asyncio.create_task(_build_graph())

    # Write initial progress
    if report_path:
        initial = build_report(
            compiles=compiled,
            compilation_output=compilation_output,
            forbidden_keyword_violations=forbidden_violations,
            results=[],
            metric_keys=metric_keys,
            detail_fields=detail_fields,
        )
        initial["progress"] = {"completed": 0, "total": total}
        _atomic_write(report_path, json.dumps(initial, indent=2))

    def _flush_progress(results: list[EvalResult], completed: int) -> None:
        if not report_path:
            return
        partial = build_report(
            compiles=compiled,
            compilation_output=compilation_output,
            forbidden_keyword_violations=forbidden_violations,
            results=results,
            metric_keys=metric_keys,
            detail_fields=detail_fields,
        )
        partial["progress"] = {"completed": completed, "total": total}
        _atomic_write(report_path, json.dumps(partial, indent=2))

    async with AsyncExitStack() as stack:

        async def _assess_one(idx: int, statement: FormalizationTarget) -> EvalResult:
            async with sem:
                try:
                    return await assess_statement(
                        idx=idx,
                        statement=statement,
                        code_dir=code_dir,
                        repo_dir=repo_dir,
                        book_dir=book_dir,
                        compilation=compilation,
                        axiom_checker=axiom_checker,
                        mathlib_cfg=mathlib_cfg,
                        inference_factory=inference_factory,
                        stack=stack,
                        trace_store=trace_store,
                        dep_graph=dep_graph_task,
                    )
                except Exception as e:
                    logger.error("Statement '%s' (idx=%d) failed: %s", statement.name, idx, e)
                    return EvalResult(
                        datum_id=statement.name,
                        score=Score(
                            value=0.0,
                            passed=False,
                            feedback=f"Pipeline error: {type(e).__name__}: {e}",
                            metrics={"compilation": 1},
                        ),
                        datum=AssessmentTarget(
                            idx=idx,
                            name=statement.name,
                            description=statement.description,
                            kind=statement.kind,
                            location=statement.location,
                            book_dir=str(book_dir),
                        ),
                    )

        tasks = [asyncio.create_task(_assess_one(target_indices[i], s)) for i, s in enumerate(targets)]

        results: list[EvalResult] = []
        completed = 0
        for coro in asyncio.as_completed(tasks):
            result = await coro
            results.append(result)
            completed += 1
            if completed % progress_batch == 0 or completed == total:
                logger.info("Progress: %d/%d statements assessed", completed, total)
                _flush_progress(results, completed)

        # as_completed yields in finish order; restore original submission order
        # so callers can safely zip results with their index/target lists.
        results.sort(key=lambda r: r.datum.idx)

    report = build_report(
        compiles=compiled,
        compilation_output=compilation_output,
        forbidden_keyword_violations=forbidden_violations,
        results=results,
        metric_keys=metric_keys,
        detail_fields=detail_fields,
    )
    report["progress"] = {"completed": total, "total": total}

    # Attach dependency info to each statement in the report
    dep_graph = await dep_graph_task
    if dep_graph:
        for detail in report.get("statements", {}).get("details", []):
            decl = detail.get("lean_declaration")
            if decl:
                node = dep_graph.get(decl)
                if node:
                    detail["deps"] = {
                        "direct": list(node.deps),
                        "transitive": list(node.all_deps),
                    }
        report["dependency_graph_size"] = dep_graph.size

    _annotate_inherited_failures(report)

    return report, results


def _annotate_inherited_failures(report: dict[str, Any]) -> None:
    """Annotate each statement with ``inherited_failure``.

    For each failed statement where ``axiom_only`` is true, check whether
    every unjustified axiom is inherited from a transitive dependency that
    is itself an evaluated statement with the same axiom in its
    ``axiom_verdicts``.  If so, the failure is inherited (the root cause
    is upstream) and ``inherited_failure`` is set to ``True``.

    Rules:
    - Passed statements: ``inherited_failure = None``
    - Failed + axiom_only + all axioms inherited: ``inherited_failure = True``,
      ``passed`` flipped to ``True``, and summary counts recomputed.
    - Otherwise: ``inherited_failure = False``
    """
    details = report.get("statements", {}).get("details", [])

    # Build lookup: lean_declaration → detail entry
    decl_to_detail: dict[str, dict[str, Any]] = {}
    for d in details:
        decl = d.get("lean_declaration")
        if decl:
            decl_to_detail[decl] = d

    for d in details:
        if d.get("passed"):
            d["inherited_failure"] = None
            continue

        if not d.get("axiom_only"):
            d["inherited_failure"] = False
            continue

        # Check if all axioms are inherited from transitive deps
        axiom_verdicts = d.get("axiom_verdicts", {})
        if not axiom_verdicts:
            d["inherited_failure"] = False
            continue

        transitive_deps = set(d.get("deps", {}).get("transitive", []))
        all_inherited = True

        for axiom_name in axiom_verdicts:
            # Check if any transitive dep is a statement with the same axiom
            inherited = False
            for dep in transitive_deps:
                dep_detail = decl_to_detail.get(dep)
                if dep_detail and axiom_name in dep_detail.get("axiom_verdicts", {}):
                    inherited = True
                    break
            if not inherited:
                all_inherited = False
                break

        d["inherited_failure"] = all_inherited
        if all_inherited:
            d["passed"] = True

    # Recompute summary counts
    summary = report.get("statements", {}).get("summary")
    if summary is not None:
        total = summary["total"]
        passed = sum(1 for d in details if d.get("passed"))
        summary["passed"] = passed
        summary["failed"] = total - passed
        summary["pass_rate"] = round(passed / total, 3) if total else 0.0
