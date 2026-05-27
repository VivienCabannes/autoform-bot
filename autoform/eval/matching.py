# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Matching agent — pairs book statements with Lean declarations."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from pathlib import Path

from core.agent import Agent
from core.agent.loader import load_agent_definition
from core.inference import InferenceProtocol
from core.trace import AgentTrace, TraceStore
from autoform.eval.lean_checks import DeclarationNotFoundError, AxiomsChecker
from autoform.eval.types import FormalizationTarget
from tools.files.filesystem.server import FilesystemConfig, filesystem_server

from .types import MatchResult

logger = logging.getLogger(__name__)

_MATCHER_DIR = Path(__file__).resolve().parent / "agents" / "matcher"


def _parse_match_response(response: str) -> MatchResult:
    """Parse the matching agent's JSON response into a MatchResult."""
    try:
        # Find the last ```json fence (agent may include examples earlier)
        fence_matches = list(re.finditer(r"```json\s*\n?([\s\S]*?)```", response))
        text = fence_matches[-1].group(1).strip() if fence_matches else response.strip()

        # Find the JSON object containing lean_declaration (not math notation like {k/2})
        json_match = re.search(r'\{"lean_declaration"[\s\S]*\}', text)
        if json_match:
            json_str = json_match.group()
        else:
            # Fallback: find the last balanced {...} block using brace counting
            json_str = None
            i = len(text) - 1
            while i >= 0:
                if text[i] == "}":
                    depth = 1
                    end = i
                    i -= 1
                    while i >= 0 and depth > 0:
                        if text[i] == "}":
                            depth += 1
                        elif text[i] == "{":
                            depth -= 1
                        i -= 1
                    if depth == 0:
                        json_str = text[i + 1 : end + 1]
                        break
                i -= 1
        if not json_str:
            return MatchResult(
                lean_declaration=None,
                lean_file=None,
                confidence="not_found",
                reasoning=f"Could not parse agent response: {response[:200]}",
            )

        data = json.loads(json_str)
        return MatchResult(
            lean_declaration=data.get("lean_declaration"),
            lean_file=data.get("lean_file"),
            confidence=data.get("confidence", "low"),
            reasoning=data.get("reasoning", ""),
        )
    except (json.JSONDecodeError, KeyError) as e:
        return MatchResult(
            lean_declaration=None,
            lean_file=None,
            confidence="not_found",
            reasoning=f"Failed to parse agent response: {e}",
        )


async def match_statement(
    statement: FormalizationTarget,
    code_dir: Path,
    book_dir: Path,
    inference_factory: Callable[[], InferenceProtocol],
    trace_store: TraceStore | None = None,
    idx: int = 0,
    axiom_checker: AxiomsChecker | None = None,
) -> MatchResult:
    """Find the Lean declaration that formalizes a book statement.

    Spins up a matching agent with filesystem tools scoped to the code
    and book directories. The agent uses grep and file reading to discover
    and match declarations.

    If *axiom_checker* is provided, validates the match by running
    ``#print axioms`` on the returned declaration. If it fails, feeds
    the error back to the agent for up to ``_MAX_RETRIES`` attempts.
    """
    definition = load_agent_definition(_MATCHER_DIR)
    server = filesystem_server(FilesystemConfig(allowed_dirs=(str(code_dir), str(book_dir))))

    prompt = (
        f"## Book Statement\n"
        f"**{statement.name}** ({statement.kind})\n"
        f"Location: {statement.location}\n\n"
        f"{statement.description}\n\n"
        f"## Lean Source Directory\n"
        f"`{code_dir}`\n\n"
        f"## Book Source Directory\n"
        f"`{book_dir}`\n\n"
        f"Find the Lean declaration that formalizes the above book statement. "
        f"Search the code directory for declarations and read the source files "
        f"to verify your match."
    )

    trace_id = f"matcher/target_{idx}"
    trace = AgentTrace(id=trace_id)

    async with Agent(
        definition=definition,
        inference=inference_factory(),
        server_configs=[server],
        trace_store=trace_store,
        id=trace_id,
        persist_dir=trace_store.run_dir if trace_store else None,
    ) as agent:
        agent.set_trace(trace)
        response = await agent.call(prompt)
        result = _parse_match_response(response)

        # Validate with axiom checker and retry until resolved (max 3 retries)
        axioms: frozenset[str] = frozenset()
        if axiom_checker and result.lean_declaration and result.confidence != "not_found":
            retries = 0
            while retries < 3:
                try:
                    all_axioms, _ = await axiom_checker.check(
                        [result.lean_declaration],
                        [result.lean_file] if result.lean_file else None,
                    )
                    axioms = all_axioms[result.lean_declaration]
                    break  # validation passed
                except DeclarationNotFoundError as e:
                    retries += 1
                    logger.warning(
                        "Axiom validation failed for '%s': %s",
                        result.lean_declaration,
                        e,
                    )
                    feedback = (
                        f"The declaration name you returned (`{result.lean_declaration}`) "
                        f"could not be resolved by `#print axioms`. Error: {e}\n\n"
                        f"Please re-read the Lean source file and check the exact "
                        f"declaration name, including any `namespace` blocks. "
                        f"Then provide a corrected response."
                    )
                    response = await agent.call(feedback)
                    result = _parse_match_response(response)
                    if result.confidence == "not_found" or result.lean_declaration is None:
                        break  # agent gave up
            else:
                # Exhausted retries — the declaration name is likely correct
                # but the .lean file was never compiled (missing .olean).
                # Keep the match so it surfaces as an issue, not "not covered".
                logger.error(
                    "Axiom validation failed after 3 retries for '%s' — file likely not built",
                    statement.name,
                )
                result = MatchResult(
                    lean_declaration=result.lean_declaration,
                    lean_file=result.lean_file,
                    confidence="file_not_built",
                    reasoning="Axiom check failed after 3 retries — .lean file may not be compiled (check root module imports)",
                )

        result = MatchResult(
            lean_declaration=result.lean_declaration,
            lean_file=result.lean_file,
            confidence=result.confidence,
            reasoning=result.reasoning,
            axioms=axioms,
        )

        agent.finalize_trace()

    if trace_store:
        trace_store.save(trace)

    logger.info(
        "Matched '%s' → %s (confidence: %s)",
        statement.name,
        result.lean_declaration or "not found",
        result.confidence,
    )
    return result
