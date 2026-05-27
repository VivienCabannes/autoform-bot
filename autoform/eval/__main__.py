#!/usr/bin/env python3

# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""CLI for formalization assessment.

Assesses whether a Lean 4 formalization codebase faithfully and correctly
captures a set of book statements. Uses:
1. Repo-level compilation and forbidden keyword checks
2. Per-statement matching (LLM agent finds Lean declaration for each book statement)
3. Per-statement axiom extraction
4. Per-statement LLM rubric grading (faithfulness, correctness, axiom justification)

Usage:
    python -m autoform.eval run \
        --repo_dir=/path/to/lean/repo \
        --code_dir=/path/to/lean/source \
        --task_file=/path/to/statements.yaml \
        --book_dir=/path/to/book/source
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

import fire
from autoform.eval.types import load_task_list
from core.inference import InferenceProtocol
from core.inference.client import create_inference, lookup_model
from core.trace import TraceStore
from dotenv import load_dotenv

from .pipeline import assess_targets

load_dotenv()
logger = logging.getLogger(__name__)

_APP_DIR = Path(__file__).resolve().parent
_MATHLIB_DIR = _APP_DIR.parent.parent / "submodules" / "mathlib"

_METRIC_KEYS = ("compilation", "faithfulness", "proof_integrity", "code_quality")
_DETAIL_FIELDS = (
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


# ---------------------------------------------------------------------------
# Cost tracking
# ---------------------------------------------------------------------------


def _build_cost_summary(trace_store: TraceStore) -> dict:
    """Load all traces and build aggregate cost summary."""
    total_input = 0
    total_output = 0
    total_cost = 0.0
    total_duration = 0.0
    trace_count = 0

    for trace_file in sorted(trace_store.run_dir.rglob("*.json")):
        try:
            with open(trace_file) as f:
                data = json.load(f)
            summary = data.get("summary", {})
            total_input += summary.get("total_input_tokens", 0)
            total_output += summary.get("total_output_tokens", 0)
            total_cost += summary.get("total_cost_usd", 0.0)
            total_duration += summary.get("total_duration_s", 0.0)
            trace_count += 1
        except (json.JSONDecodeError, OSError):
            continue

    return {
        "agents": trace_count,
        "input_tokens": total_input,
        "output_tokens": total_output,
        "cost_usd": round(total_cost, 4),
        "duration_s": round(total_duration, 2),
    }


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


async def _run(
    repo_dir: str,
    code_dir: str,
    task_file: str,
    book_dir: str,
    *,
    model: str = "Opus 4.6",
    concurrency: int = 1000,
    skip_compilation: bool = False,
    trace_dir: str | None = None,
    report_path: str | None = None,
) -> tuple[dict[str, Any], TraceStore]:
    repo = Path(repo_dir)
    code = Path(code_dir)
    book = Path(book_dir)
    model_def = lookup_model(model)

    def inference_factory() -> InferenceProtocol:
        return create_inference(model_def)

    statements = load_task_list(Path(task_file))
    logger.info("Loaded %d statements from %s", len(statements), task_file)

    trace_store = TraceStore.create_run(
        Path(trace_dir) if trace_dir else _APP_DIR / "output" / "traces"
    )
    logger.info("Traces will be saved to %s", trace_store.run_dir)

    report, _results = await assess_targets(
        targets=statements,
        code_dir=code,
        repo_dir=repo,
        book_dir=book,
        mathlib_path=_MATHLIB_DIR,
        inference_factory=inference_factory,
        concurrency=concurrency,
        skip_compilation=skip_compilation,
        trace_store=trace_store,
        metric_keys=_METRIC_KEYS,
        detail_fields=_DETAIL_FIELDS,
        report_path=Path(report_path) if report_path else None,
    )
    return report, trace_store


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def run(
    repo_dir: str,
    code_dir: str,
    task_file: str,
    book_dir: str,
    *,
    model: str = "Opus 4.6",
    concurrency: int = 1000,
    skip_compilation: bool = False,
    report_path: str | None = None,
) -> None:
    """Assess the quality of a Lean 4 formalization against book statements.

    Args:
        repo_dir: Path to the Lean repository root (where lakefile.toml lives).
        code_dir: Path to the Lean source directory to evaluate
            (e.g. 'Atlas/HighDimensionalStatistics').
        task_file: Path to the YAML task list (name + description per statement).
        book_dir: Path to the book source directory.
        model: LLM model to use for matching and judging.
        concurrency: Max concurrent per-statement assessment tasks.
        skip_compilation: Skip the compilation gate entirely. Useful for testing
            matching and grading when the build is known to be broken.
        report_path: Path to write progressive report.json updates and
            dependency_graph.json. If not provided, report is only written
            at the end.
    """
    output_dir = _APP_DIR / "output"
    output_dir.mkdir(exist_ok=True)
    log_file = output_dir / "run.log"
    logging.basicConfig(
        level=logging.INFO,
        handlers=[
            logging.StreamHandler(sys.stderr),
            logging.FileHandler(log_file, mode="w"),
        ],
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    start_time = time.time()
    report, trace_store = asyncio.run(
        _run(
            repo_dir,
            code_dir,
            task_file,
            book_dir,
            model=model,
            concurrency=concurrency,
            skip_compilation=skip_compilation,
            report_path=report_path,
        )
    )
    wall_clock_s = round(time.time() - start_time, 2)

    # Write outputs to the run directory (alongside traces)
    if trace_store:
        run_dir = trace_store.run_dir
    else:
        run_dir = output_dir
    run_dir.mkdir(parents=True, exist_ok=True)

    output_file = run_dir / "report.json"
    output_file.write_text(json.dumps(report, indent=2))
    logger.info("Report written to %s", output_file)

    if trace_store:
        cost = _build_cost_summary(trace_store)
        cost["wall_clock_s"] = wall_clock_s
        logger.info(
            "Cost: %d agents, %d input tokens, %d output tokens, $%.4f USD, %.1fs wall-clock",
            cost["agents"],
            cost["input_tokens"],
            cost["output_tokens"],
            cost["cost_usd"],
            wall_clock_s,
        )
        cost_path = run_dir / "cost.json"
        cost_path.write_text(json.dumps(cost, indent=2))
        logger.info("Cost summary saved to %s", cost_path)

    # Generate markdown report + failed_targets.yaml
    from .generate_report import generate

    generate(str(output_file), targets_path=task_file)


if __name__ == "__main__":
    fire.Fire({"run": run})
