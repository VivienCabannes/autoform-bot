# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Per-chunk statement extraction using an LLM agent."""

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
from .parsing import parse_yaml_response
from autoform.eval.types import FormalizationTarget

logger = logging.getLogger(__name__)

_EXTRACTOR_DIR = Path(__file__).resolve().parent / "agents" / "extractor"


def _parse_statements(response: str) -> list[FormalizationTarget]:
    """Parse YAML response into FormalizationTarget objects."""
    entries = parse_yaml_response(response)
    if not entries:
        return []

    statements: list[FormalizationTarget] = []
    for entry in entries:
        if not isinstance(entry, dict) or "name" not in entry:
            continue
        statements.append(
            FormalizationTarget(
                name=str(entry["name"]),
                description=str(entry.get("description", "")),
                location=str(entry.get("location", "")),
                kind=str(entry.get("kind", "")),
            )
        )
    return statements


async def extract_from_chunk(
    chunk: Chunk,
    inference_factory: Callable[[], InferenceProtocol],
    semaphore: asyncio.Semaphore | None = None,
    trace_store: TraceStore | None = None,
    agent_index: int = 0,
) -> list[FormalizationTarget]:
    """Extract statements from a single chunk using one LLM agent."""
    definition = load_agent_definition(_EXTRACTOR_DIR)

    prompt = (
        f"Extract all labeled mathematical statements from the following text.\n"
        f"Lines {chunk.start_line}-{chunk.end_line}\n\n"
        f"---\n{chunk.text}\n---"
    )

    trace_id = f"chunk_{chunk.index}/agent_{agent_index}"

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

    statements = _parse_statements(response)
    logger.info(
        "Extracted %d statements from chunk %d agent %d (lines %d-%d)",
        len(statements),
        chunk.index,
        agent_index,
        chunk.start_line,
        chunk.end_line,
    )
    return statements


async def extract_k_from_chunk(
    chunk: Chunk,
    inference_factory: Callable[[], InferenceProtocol],
    k: int = 4,
    semaphore: asyncio.Semaphore | None = None,
    trace_store: TraceStore | None = None,
) -> list[list[FormalizationTarget]]:
    """Run k independent extractions on the same chunk."""
    results = await asyncio.gather(
        *(extract_from_chunk(chunk, inference_factory, semaphore, trace_store, agent_index=i) for i in range(k))
    )
    return list(results)
