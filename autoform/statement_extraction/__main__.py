#!/usr/bin/env python3

# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""CLI for statement extraction from mathematical textbooks.

Pipeline:
1. Discover + chunk book files
2. Extract: k agents per chunk (parallel)
3. Reconcile: within-chunk consensus + reviewer for disputes
4. Merge: deduplicate across adjacent overlapping chunks
5. Write: targets.yaml + cost.json

Usage:
    python -m autoform.statement_extraction run \
        --book_dir=autoform/data/algebraic_topology_I
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

import fire
import yaml
from dotenv import load_dotenv

from core.inference import InferenceProtocol
from core.trace import TraceStore
from autoform.eval.types import save_task_list
from core.inference.client import create_inference, lookup_model

from .chunking import chunk_all
from .extraction import extract_k_from_chunk
from .merging import merge_all
from .reconciliation import ChunkConflict, reconcile_all

load_dotenv()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Reporting utilities
# ---------------------------------------------------------------------------


def _write_review(conflicts: list[ChunkConflict], output_path: Path) -> None:
    """Write a review file for chunks with disagreements."""
    entries: list[dict] = []
    for conflict in conflicts:
        chunk_entry: dict = {
            "chunk_index": conflict.chunk.index,
            "lines": f"{conflict.chunk.start_line}-{conflict.chunk.end_line}",
            "disputed_statements": [],
        }
        for d in conflict.disputed:
            dispute: dict = {
                "name": d.name,
                "reason": d.reason,
                "found_by_agents": d.found_by,
                "versions": [{"name": v.name, "description": v.description, "kind": v.kind} for v in d.versions],
            }
            chunk_entry["disputed_statements"].append(dispute)
        entries.append(chunk_entry)

    output_path.write_text(
        yaml.dump(entries, default_flow_style=False, allow_unicode=True, width=120),
        encoding="utf-8",
    )


def _build_cost_summary(trace_store: TraceStore) -> dict:
    """Load all traces and build aggregate cost summary."""
    total_input = 0
    total_output = 0
    total_cost = 0.0
    trace_count = 0

    for trace_file in sorted(trace_store.run_dir.rglob("*.json")):
        try:
            with open(trace_file) as f:
                data = json.load(f)
            summary = data.get("summary", {})
            total_input += summary.get("total_input_tokens", 0)
            total_output += summary.get("total_output_tokens", 0)
            total_cost += summary.get("total_cost_usd", 0.0)
            trace_count += 1
        except (json.JSONDecodeError, OSError):
            continue

    return {
        "agents": trace_count,
        "input_tokens": total_input,
        "output_tokens": total_output,
        "cost_usd": round(total_cost, 4),
    }


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


async def _run(
    book_dir: str,
    *,
    model: str = "Opus 4.6",
    k: int = 4,
    chunk_size: int = 500,
    overlap: int = 50,
    concurrency: int = 5,
    output: str | None = None,
) -> None:
    book = Path(book_dir)
    model_def = lookup_model(model)
    output_path = Path(output) if output else book / "targets.yaml"

    trace_store = TraceStore.create_run(output_path.parent / "traces")
    logger.info("Traces: %s", trace_store.run_dir)

    def inference_factory() -> InferenceProtocol:
        return create_inference(model_def)

    semaphore = asyncio.Semaphore(concurrency)

    # Step 1: Discover and chunk
    chunks = chunk_all(book, chunk_size=chunk_size, overlap=overlap)
    logger.info(
        "Processing %d chunks × %d agents = %d total extractions (concurrency=%d)",
        len(chunks),
        k,
        len(chunks) * k,
        concurrency,
    )

    # Step 2: Extract — k agents per chunk, all in parallel
    all_extractions = list(
        await asyncio.gather(
            *(
                extract_k_from_chunk(c, inference_factory, k=k, semaphore=semaphore, trace_store=trace_store)
                for c in chunks
            )
        )
    )

    # Step 3: Reconcile — within-chunk consensus + review
    chunk_statements, conflicts = await reconcile_all(
        chunks, all_extractions, inference_factory, semaphore, trace_store
    )

    # Step 4: Merge — deduplicate across adjacent overlapping chunks
    statements = await merge_all(chunks, chunk_statements, inference_factory, semaphore, trace_store)

    # Step 5: Write outputs
    save_task_list(statements, output_path)
    logger.info("Wrote %d statements to %s", len(statements), output_path)

    if conflicts:
        review_path = output_path.with_name("review.yaml")
        _write_review(conflicts, review_path)
        total_disputed = sum(len(c.disputed) for c in conflicts)
        logger.info(
            "%d chunks had conflicts (%d disputed statements) — see %s",
            len(conflicts),
            total_disputed,
            review_path,
        )

    # Cost summary
    cost = _build_cost_summary(trace_store)
    logger.info(
        "Cost: %d agents, %d input tokens, %d output tokens, $%.4f USD",
        cost["agents"],
        cost["input_tokens"],
        cost["output_tokens"],
        cost["cost_usd"],
    )
    cost_path = output_path.with_name("cost.json")
    cost_path.write_text(json.dumps(cost, indent=2), encoding="utf-8")
    logger.info("Cost summary saved to %s", cost_path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def run(
    book_dir: str,
    *,
    model: str = "Opus 4.6",
    k: int = 4,
    chunk_size: int = 500,
    overlap: int = 50,
    concurrency: int = 5,
    output: str | None = None,
) -> None:
    """Extract mathematical statements from a textbook.

    Args:
        book_dir: Path to the book directory containing .md or .tex files.
        model: LLM model to use for extraction.
        k: Number of independent extractions per chunk.
        chunk_size: Lines per chunk.
        overlap: Overlapping lines between consecutive chunks.
        concurrency: Max concurrent extraction agents.
        output: Output file path. Defaults to <book_dir>/targets.yaml.
    """
    logging.basicConfig(
        level=logging.INFO,
        handlers=[logging.StreamHandler(sys.stderr)],
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(
        _run(book_dir, model=model, k=k, chunk_size=chunk_size, overlap=overlap, concurrency=concurrency, output=output)
    )


if __name__ == "__main__":
    fire.Fire({"run": run})
