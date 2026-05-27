# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Reconciliation — resolve k-agent disagreements within a single chunk.

Takes k independent extraction results for one chunk, identifies where
agents agree and disagree, and uses a reviewer agent to settle disputes.
Produces a single curated statement list per chunk.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from core.agent import Agent
from core.agent.loader import load_agent_definition
from core.inference import InferenceProtocol
from core.trace import AgentTrace, TraceStore

from .chunking import Chunk
from .normalization import normalize_statement_name
from .parsing import parse_yaml_response
from autoform.eval.types import FormalizationTarget

logger = logging.getLogger(__name__)

_REVIEWER_DIR = Path(__file__).resolve().parent / "agents" / "reviewer"


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DisputedFormalizationTarget:
    """A statement where agents disagreed."""

    name: str
    found_by: list[int]
    versions: list[FormalizationTarget]
    reason: str


@dataclass(frozen=True)
class ChunkConflict:
    """A chunk where the k extractions disagreed."""

    chunk: Chunk
    extractions: list[list[FormalizationTarget]]
    agreed: list[FormalizationTarget]
    disputed: list[DisputedFormalizationTarget]


# ---------------------------------------------------------------------------
# Consensus check
# ---------------------------------------------------------------------------


def _find_consensus(
    chunk: Chunk,
    extractions: list[list[FormalizationTarget]],
) -> tuple[list[FormalizationTarget], ChunkConflict | None]:
    """Compare k extractions for a single chunk.

    A statement is accepted if ALL k agents found it.
    FormalizationTargets found by fewer than k agents are disputed.
    """
    k = len(extractions)

    by_agent: list[dict[str, FormalizationTarget]] = []
    for agent_stmts in extractions:
        index: dict[str, FormalizationTarget] = {}
        for s in agent_stmts:
            key = normalize_statement_name(s.name)
            index[key] = s
        by_agent.append(index)

    all_names: dict[str, None] = {}
    for index in by_agent:
        for key in index:
            all_names[key] = None

    agreed: list[FormalizationTarget] = []
    disputed: list[DisputedFormalizationTarget] = []

    for name_key in all_names:
        found_by: list[int] = []
        versions: list[FormalizationTarget] = []
        for i, index in enumerate(by_agent):
            if name_key in index:
                found_by.append(i)
                versions.append(index[name_key])

        if len(found_by) < k:
            disputed.append(
                DisputedFormalizationTarget(
                    name=versions[0].name,
                    found_by=found_by,
                    versions=versions,
                    reason=f"Found by {len(found_by)}/{k} agents",
                )
            )
        else:
            agreed.append(versions[0])

    conflict = None
    if disputed:
        conflict = ChunkConflict(
            chunk=chunk,
            extractions=extractions,
            agreed=agreed,
            disputed=disputed,
        )

    return agreed, conflict


# ---------------------------------------------------------------------------
# Reviewer agent
# ---------------------------------------------------------------------------


def _format_disputed(conflict: ChunkConflict) -> str:
    """Format disputed statements for the reviewer prompt."""
    lines: list[str] = []
    for d in conflict.disputed:
        lines.append(f"### {d.name}")
        lines.append(f"Found by agents: {d.found_by} (out of {len(conflict.extractions)})")
        for i, v in enumerate(d.versions):
            lines.append(f"\n**Agent {d.found_by[i]} version:**")
            lines.append(f"- name: {v.name}")
            lines.append(f"- description: {v.description}")
            lines.append(f"- kind: {v.kind}")
        lines.append("")
    return "\n".join(lines)


async def _review_chunk(
    conflict: ChunkConflict,
    inference_factory: Callable[[], InferenceProtocol],
    semaphore: asyncio.Semaphore | None = None,
    trace_store: TraceStore | None = None,
) -> list[FormalizationTarget]:
    """Review disputed statements in a single chunk."""
    definition = load_agent_definition(_REVIEWER_DIR)
    disputed_text = _format_disputed(conflict)

    prompt = (
        f"## Source Text\n"
        f"Lines {conflict.chunk.start_line}-{conflict.chunk.end_line}\n\n"
        f"---\n{conflict.chunk.text}\n---\n\n"
        f"## Disputed FormalizationTargets\n\n"
        f"{disputed_text}\n\n"
        f"Review each disputed statement against the source text and decide: include or exclude."
    )

    trace_id = f"chunk_{conflict.chunk.index}/reviewer"

    if semaphore:
        await semaphore.acquire()
    try:
        async with Agent(
            definition=definition,
            inference=inference_factory(),
            trace_store=trace_store,
            id=trace_id,
        ) as agent:
            trace = AgentTrace(id=trace_id)
            agent.set_trace(trace)
            response = await agent.call(prompt)
            agent.finalize_trace()
    finally:
        if semaphore:
            semaphore.release()

    entries = parse_yaml_response(response)
    if not entries:
        entries = []

    included: list[FormalizationTarget] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("verdict", "").lower() != "include":
            logger.info("Reviewer excluded: %s — %s", entry.get("name", "?"), entry.get("reason", ""))
            continue
        included.append(
            FormalizationTarget(
                name=str(entry.get("name", "")),
                description=str(entry.get("description", "")),
                location=str(entry.get("location", "")),
                kind=str(entry.get("kind", "")),
            )
        )

    logger.info(
        "Reviewer resolved chunk %d: %d/%d disputed statements included",
        conflict.chunk.index,
        len(included),
        len(conflict.disputed),
    )
    return included


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def reconcile_chunk(
    chunk: Chunk,
    extractions: list[list[FormalizationTarget]],
    inference_factory: Callable[[], InferenceProtocol],
    semaphore: asyncio.Semaphore | None = None,
    trace_store: TraceStore | None = None,
) -> tuple[list[FormalizationTarget], ChunkConflict | None]:
    """Reconcile k extractions for a single chunk.

    1. Find consensus — statements all k agents agree on.
    2. Review disputes — reviewer agent decides include/exclude.
    3. Return the curated list (consensus + reviewer-included).
    """
    agreed, conflict = _find_consensus(chunk, extractions)

    if not conflict:
        return agreed, None

    reviewed = await _review_chunk(conflict, inference_factory, semaphore, trace_store)
    return agreed + reviewed, conflict


async def reconcile_all(
    chunks: list[Chunk],
    all_extractions: list[list[list[FormalizationTarget]]],
    inference_factory: Callable[[], InferenceProtocol],
    semaphore: asyncio.Semaphore | None = None,
    trace_store: TraceStore | None = None,
) -> tuple[list[list[FormalizationTarget]], list[ChunkConflict]]:
    """Reconcile all chunks in parallel.

    Returns:
        - Per-chunk curated statement lists.
        - List of chunks that had conflicts (for reporting).
    """
    results = await asyncio.gather(
        *(
            reconcile_chunk(chunk, extractions, inference_factory, semaphore, trace_store)
            for chunk, extractions in zip(chunks, all_extractions)
        )
    )

    chunk_statements: list[list[FormalizationTarget]] = []
    all_conflicts: list[ChunkConflict] = []
    for (statements, conflict), chunk in zip(results, chunks):
        chunk_statements.append(statements)
        if conflict:
            all_conflicts.append(conflict)
        logger.info(
            "  chunk %d (lines %d-%d): %d statements",
            chunk.index,
            chunk.start_line,
            chunk.end_line,
            len(statements),
        )

    total = sum(len(s) for s in chunk_statements)
    logger.info(
        "Reconciliation: %d statements across %d chunks, %d chunks had conflicts",
        total,
        len(chunks),
        len(all_conflicts),
    )
    return chunk_statements, all_conflicts
