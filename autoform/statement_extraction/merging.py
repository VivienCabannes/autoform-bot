# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Merging — deduplicates statements across adjacent overlapping chunks."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
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

_MERGER_DIR = Path(__file__).resolve().parent / "agents" / "merger"


def _needs_merging(
    chunk_a: Chunk,
    chunk_b: Chunk,
    statements_a: list[FormalizationTarget],
    statements_b: list[FormalizationTarget],
) -> bool:
    """Check if two adjacent chunks need merging.

    Only needed if they overlap and both have statements.
    """
    if chunk_b.start_line > chunk_a.end_line:
        return False  # No overlap
    if not statements_a or not statements_b:
        return False
    return True


async def merge_pair(
    chunk_a: Chunk,
    chunk_b: Chunk,
    statements_a: list[FormalizationTarget],
    statements_b: list[FormalizationTarget],
    inference_factory: Callable[[], InferenceProtocol],
    semaphore: asyncio.Semaphore | None = None,
    trace_store: TraceStore | None = None,
) -> list[FormalizationTarget]:
    """Reconcile statements between two adjacent overlapping chunks.

    Returns the deduplicated list of statements from chunk_b — only
    genuinely new statements (not duplicates of chunk_a's statements).
    """
    definition = load_agent_definition(_MERGER_DIR)

    # Format the prompt
    def _fmt(stmts: list[FormalizationTarget]) -> str:
        parts = []
        for s in stmts:
            parts.append(f'- name: "{s.name}"\n  description: "{s.description}"')
        return "\n".join(parts)

    stmts_a_text = _fmt(statements_a)
    stmts_b_text = _fmt(statements_b)
    names_a = ", ".join(s.name for s in statements_a) or "(none)"
    names_b = ", ".join(s.name for s in statements_b) or "(none)"
    prompt = (
        f"## Statements from earlier chunk (chunk {chunk_a.index}, lines {chunk_a.start_line}-{chunk_a.end_line})\n"
        f"{stmts_a_text}\n\n"
        f"## Statements from later chunk (chunk {chunk_b.index}, lines {chunk_b.start_line}-{chunk_b.end_line})\n"
        f"{stmts_b_text}\n\n"
        f"The earlier chunk has {len(statements_a)} statement(s): {names_a}\n"
        f"The later chunk has {len(statements_b)} statement(s): {names_b}\n\n"
        f"Start by listing the names from each chunk, then for each statement from the later chunk, decide: duplicate or new?"
    )

    trace_id = f"merge_{chunk_a.index}_{chunk_b.index}"

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

    # Parse response
    entries = parse_yaml_response(response)

    if not entries:
        logger.info(
            "Merged chunks %d-%d: no duplicates, all %d statements from chunk %d are new",
            chunk_a.index,
            chunk_b.index,
            len(statements_b),
            chunk_b.index,
        )
        return statements_b

    # Build set of duplicate names from chunk_b
    duplicate_names: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        # Skip entries marked as new/keep — only process duplicates
        verdict = str(entry.get("verdict", "")).lower()
        if verdict in ("new", "keep", "not duplicate", "not a duplicate"):
            continue
        name = str(entry.get("name", ""))
        if not name:
            continue
        duplicate_names.add(normalize_statement_name(name))
        # Also add the part before ":" in case the model concatenated name and description
        if ":" in name:
            prefix = name.split(":", 1)[0].strip()
            if prefix:
                duplicate_names.add(normalize_statement_name(prefix))
        logger.info(
            "  drop: %s — %s",
            name,
            entry.get("reason", "no reason given"),
        )

    # Filter chunk_b's statements — remove only the first occurrence of each duplicate
    remaining_drops: dict[str, int] = {}
    for norm in duplicate_names:
        remaining_drops[norm] = remaining_drops.get(norm, 0) + 1

    kept: list[FormalizationTarget] = []
    for s in statements_b:
        norm = normalize_statement_name(s.name)
        if norm in remaining_drops and remaining_drops[norm] > 0:
            remaining_drops[norm] -= 1
            continue
        kept.append(s)

    logger.info(
        "Merged chunks %d-%d: %d/%d statements from chunk %d are new",
        chunk_a.index,
        chunk_b.index,
        len(kept),
        len(statements_b),
        chunk_b.index,
    )
    return kept


async def merge_all(
    chunks: list[Chunk],
    chunk_statements: list[list[FormalizationTarget]],
    inference_factory: Callable[[], InferenceProtocol],
    semaphore: asyncio.Semaphore | None = None,
    trace_store: TraceStore | None = None,
) -> list[FormalizationTarget]:
    """Merge all adjacent chunk pairs and produce the final statement list.

    Each merge_pair only reads the original chunk statements (not accumulated
    results), so all pairs run in parallel.
    """
    if not chunks:
        return []

    # Launch all merge pairs in parallel — each is independent since
    # merge_pair reads original chunk_statements, not accumulated results.
    async def _process_pair(i: int) -> list[FormalizationTarget]:
        if not _needs_merging(chunks[i - 1], chunks[i], chunk_statements[i - 1], chunk_statements[i]):
            return chunk_statements[i]
        return await merge_pair(
            chunks[i - 1],
            chunks[i],
            chunk_statements[i - 1],
            chunk_statements[i],
            inference_factory,
            semaphore,
            trace_store,
        )

    pair_results = await asyncio.gather(*(_process_pair(i) for i in range(1, len(chunks))))

    # Assemble: chunk 0's statements + deduplicated statements from each subsequent chunk
    result: list[FormalizationTarget] = list(chunk_statements[0])
    for statements in pair_results:
        result.extend(statements)

    logger.info("Merge complete: %d total statements", len(result))
    return result
