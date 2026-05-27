# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""LeanConcurrentAgents — Lean-specific build/review overrides for ConcurrentAgents."""

from __future__ import annotations

import asyncio
import logging
import subprocess
from collections.abc import Callable
from pathlib import Path

from core.agent import Agent, AgentDefinition, load_agent_definition
from core.inference import InferenceProtocol
from core.task import Task
from core.trace import AgentTrace, TraceStore, traced
from core.coordination.concurrent_agents import ConcurrentAgents
from core.coordination.merge_queue import MergeQueueClient, MergeStatus
from tools import resolve_servers
from tools.files.filesystem.server import FilesystemConfig

logger = logging.getLogger(__name__)

DEFAULT_MAX_CONCURRENT_BUILDS = 8
_INSPECTOR_DIR = Path(__file__).resolve().parent / "agents" / "quality_inspector"


def _update_root_imports(repo: Path) -> None:
    """Regenerate root module to import all .lean files in the lib directory.

    The root module (e.g. ``Algebraic_Topology_II.lean``) must import every
    section file so that ``lake build`` compiles them all.  Without this,
    newly added sections have no ``.olean`` and ``#print axioms`` fails.
    """
    try:
        from core.compat import tomllib
    except ImportError:
        import tomllib  # type: ignore[no-redef]

    lakefile = repo / "lakefile.toml"
    if not lakefile.exists():
        return
    with open(lakefile, "rb") as f:
        cfg = tomllib.load(f)
    libs = cfg.get("lean_lib", [])
    if not libs:
        return
    lib_name = libs[0]["name"]

    lib_dir = repo / lib_name
    root_module = repo / f"{lib_name}.lean"
    if not lib_dir.is_dir():
        return

    section_files = sorted(lib_dir.rglob("*.lean"))
    imports = []
    for f in section_files:
        module = str(f.relative_to(repo)).removesuffix(".lean").replace("/", ".")
        imports.append(f"import {module}")
    new_content = "\n".join(imports) + "\n" if imports else ""

    if root_module.exists() and root_module.read_text() == new_content:
        return

    root_module.write_text(new_content)

    subprocess.run(
        ["git", "add", str(root_module)],
        cwd=repo,
        capture_output=True,
        timeout=30,
    )
    subprocess.run(
        ["git", "commit", "-m", "update root imports"],
        cwd=repo,
        capture_output=True,
        timeout=30,
    )
    logger.info("Updated root imports: %d modules in %s", len(imports), root_module.name)


class LeanConcurrentAgents(ConcurrentAgents):
    """ConcurrentAgents with Lean-specific build check and concurrent review.

    Review runs the pool's reviewer (correctness) and a quality inspector
    concurrently. Both must approve for the review to pass.
    """

    def __init__(
        self,
        repo_root: Path | None = None,
        max_concurrent_builds: int = DEFAULT_MAX_CONCURRENT_BUILDS,
        inference_factory: Callable[[], InferenceProtocol] | None = None,
        allowed_paths: list[Path] | None = None,
        trace_store: TraceStore | None = None,
        merge_client: MergeQueueClient | None = None,
        max_review_cycles: int = 0,
    ):
        super().__init__(repo_root=repo_root, max_review_cycles=max_review_cycles)
        self._build_semaphore = asyncio.Semaphore(max_concurrent_builds)
        self._inference_factory = inference_factory
        self._allowed_paths = allowed_paths or []
        self._trace_store = trace_store
        self._merge_client = merge_client
        self._inspector_def: AgentDefinition | None = (
            load_agent_definition(_INSPECTOR_DIR) if _INSPECTOR_DIR.exists() else None
        )

    async def _do_merge(
        self,
        agent: Agent,
    ) -> tuple[bool, str | None, str | None, str | None]:
        """Delegate merge to the coordinator's MergeQueue if available."""
        if not self._merge_client:
            return await super()._do_merge(agent)

        wt_path = getattr(agent, "worktree_path", None)
        if not wt_path:
            return await super()._do_merge(agent)

        # Record a step immediately so the visualizer shows the queue submission.
        from core.trace.step_trace import _current_step_ctx, StepRecord
        import time

        ctx = _current_step_ctx.get(None)
        if ctx is not None:
            ctx._record(
                StepRecord(
                    function="merge_queue_submit",
                    timestamp=time.time(),
                    duration_ms=0,
                    success=True,
                    args_summary={"agent": agent.id, "worktree": str(wt_path)},
                )
            )

        result = await self._merge_client.submit(agent.id, Path(wt_path))
        if result.status == MergeStatus.MERGED:
            return True, result.pre_hash, result.post_hash, None
        return False, None, None, result.error

    @traced
    async def build(self, agent: Agent, task: Task) -> tuple[bool, str]:
        """Update root imports and run lake build on the agent's worktree."""
        wt_path = getattr(agent, "worktree_path", None)
        if not wt_path:
            return True, ""

        # Ensure root module imports all section files before building
        await asyncio.to_thread(_update_root_imports, Path(wt_path))

        async with self._build_semaphore:
            result = await asyncio.to_thread(
                subprocess.run,
                f"cd {wt_path} && lake build",
                shell=True,
                capture_output=True,
                text=True,
                timeout=3600,
            )
        if result.returncode != 0:
            output = result.stdout + result.stderr
            if "Permission denied" in output or "Read-only" in output:
                logger.error(
                    "FROZEN ARTIFACT WRITE DETECTED in %s:\n%s",
                    wt_path,
                    output[:2000],
                )
            # Show both head and tail so agents see early context AND the
            # most recent (often most relevant) errors at the end.
            half = 1500
            if len(output) <= half * 2:
                feedback = output
            else:
                feedback = (
                    output[:half] + f"\n\n... [{len(output) - half * 2} chars truncated] ...\n\n" + output[-half:]
                )
            return False, feedback
        return True, ""

    def _build_review_prompt(self, agent: Agent, task: Task) -> str:
        wt_path = getattr(agent, "worktree_path", "unknown")
        return (
            f"Review the changes in this worktree: {wt_path}\n\n"
            f"Task ID: {task.id}\n"
            f"Objective: {task.title}\n"
            f"Original task: {task.description}\n\n"
            f"NOTE: Compilation has already been verified by the build step. "
            f"Do NOT run lake build or lean_diagnostic_messages.\n\n"
            f"## Review Steps\n\n"
            f"1. **Check the diff**: Run `git diff HEAD~1` in bash to see changes\n"
            f"2. **Read the original source**: Open the relevant section in `book/` (LaTeX or Markdown) and read the "
            f"theorem/definition statement directly — do not rely on the task prompt's description of it.\n"
            f"3. **Verify the statement matches**: The Lean statement must match the book's statement exactly. "
            f"Extra hypotheses not in the book are deviations — reject unless they are provably redundant.\n"
            f"4. **Check proof approach**: Is the proof strategy sound?\n"
            f"5. **Check for remaining sorry**: Are there any sorry's that should have been proved?\n\n"
            f"## Response Format\n\n"
            f"APPROVED: <brief reason>  or  REJECTED: <specific, actionable feedback>"
        )

    def _build_inspect_prompt(self, agent: Agent, task: Task) -> str:
        wt_path = getattr(agent, "worktree_path", "unknown")
        return (
            f"Inspect the code quality of the changes in this worktree: {wt_path}\n\n"
            f"Task ID: {task.id}\n"
            f"Objective: {task.title}\n"
            f"Original task: {task.description}\n\n"
            f"NOTE: Compilation has already been verified. Do NOT run lake build.\n\n"
            f"and evaluate them against the Mathlib conventions in your system prompt.\n\n"
            f"APPROVED: <brief reason>  or  REJECTED: <specific, actionable feedback>"
        )

    @traced
    async def review(
        self,
        agent: Agent,
        reviewer: Agent | None,
        task: Task,
    ) -> tuple[bool, str]:
        """Run reviewer and quality inspector concurrently. Both must approve."""
        if not reviewer:
            return True, ""

        # Run the correctness reviewer
        review_prompt = self._build_review_prompt(agent, task)
        review_coro = reviewer.call(review_prompt)

        # Create and run the quality inspector (if available)
        inspect_coro = None
        inspector: Agent | None = None
        inspector_trace: AgentTrace | None = None

        if self._inspector_def and self._inference_factory:
            wt_path = getattr(agent, "worktree_path", "unknown")
            extra = [str(p) for p in self._allowed_paths]
            packages_ro = (str(Path(wt_path) / ".lake" / "packages"),)
            inspector_servers = resolve_servers(
                self._inspector_def.tool_servers,
                workspace=wt_path,
                filesystem=FilesystemConfig(
                    allowed_dirs=tuple([wt_path] + extra),
                    write_excluded_dirs=packages_ro,
                ),
            )

            inspector_id = f"{agent.id}-inspector"
            inspector = Agent(
                definition=self._inspector_def,
                inference=self._inference_factory(),
                server_configs=inspector_servers,
                trace_store=self._trace_store,
                id=inspector_id,
                persist_dir=Path(wt_path),
            )
            await inspector.__aenter__()

            inspector_trace = AgentTrace(id=inspector_id, task_id=task.id)
            # Derive attempt prefix from reviewer's trace to save alongside it
            reviewer_trace = getattr(reviewer, "_trace", None)
            if reviewer_trace and hasattr(reviewer_trace, "trace_id") and reviewer_trace.trace_id:
                attempt_prefix = reviewer_trace.trace_id.rsplit("/", 1)[0]
                inspector_trace.trace_id = f"{attempt_prefix}/{inspector_id}"
            inspector.set_trace(inspector_trace)

            inspect_prompt = self._build_inspect_prompt(agent, task)
            inspect_coro = inspector.call(inspect_prompt)

        try:
            if inspect_coro:
                review_answer, inspect_answer = await asyncio.gather(review_coro, inspect_coro)
            else:
                review_answer = await review_coro
                inspect_answer = None
        finally:
            # Finalize inspector trace and close
            if inspector:
                if inspector_trace:
                    inspector_trace.finalize(
                        status="completed",
                        total_turns=inspector.total_turns,
                        messages=inspector.messages,
                    )
                    if self._trace_store:
                        self._trace_store.save(inspector_trace)
                await inspector.close()

        # Parse results
        review_ok = bool(review_answer) and "APPROVED" in review_answer.upper()
        inspect_ok = not inspect_answer or "APPROVED" in inspect_answer.upper()

        # Merge feedback
        parts = []
        if review_answer:
            parts.append(f"--- Correctness Review ---\n{review_answer}")
        if inspect_answer:
            parts.append(f"--- Quality Inspection ---\n{inspect_answer}")
        combined = "\n\n".join(parts)

        if not review_ok:
            return False, combined
        if not inspect_ok:
            return False, combined
        return True, combined
