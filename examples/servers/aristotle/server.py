"""Aristotle (Harmonic) MCP server — delegate formalization tasks to an autonomous prover.

Aristotle is not a chat LLM. It is an autonomous formal-reasoning agent that
takes a prompt (and optionally a Lean project directory), runs its own internal
tools (proof search, Lean builds, file edits), and returns finished Lean files
plus a natural-language summary.

This MCP server wraps ``aristotlelib`` to let any coding assistant delegate
formalization tasks to Aristotle as a tool call. The assistant submits a task,
polls for completion, retrieves results, and optionally steers a running task.

Dependency: ``aristotlelib`` (provided by the ``aristotle`` extra in pyproject.toml).
API key: ARISTOTLE_API_KEY env var (mint at https://aristotle.harmonic.fun/dashboard/keys)
"""

from __future__ import annotations

import asyncio
import json
import os
import tarfile
import time
from dataclasses import dataclass, field
from logging import getLogger
from pathlib import Path
from typing import Any

from fastmcp.server import FastMCP

logger = getLogger(__name__)

# Status classification (compared by string value, enum-agnostic)
_IN_FLIGHT_STATUSES = frozenset({"QUEUED", "IN_PROGRESS"})
_CONTINUABLE_STATUSES = frozenset({"COMPLETE_WITH_ERRORS", "OUT_OF_BUDGET"})


def _status_value(status: Any) -> str:
    """Return the .value of a TaskStatus (or the string itself)."""
    return str(getattr(status, "value", status))


@dataclass
class AristotleSession:
    """Manages one Aristotle project session.

    A session wraps a single ``aristotlelib.Project`` and tracks its tasks.
    Multiple sessions can coexist (one per formalization target).
    """

    project: Any = None
    project_id: str = ""
    last_task: Any = None
    last_status: str = ""
    created_at: float = 0.0
    events_seen: set[str] = field(default_factory=set)


class AristotleManager:
    """Manages multiple Aristotle sessions for concurrent task delegation.

    Each session is identified by a user-chosen session_id (e.g., the
    theorem name or target ID). Sessions are independent and can run
    in parallel.
    """

    def __init__(self, *, download_dir: str | None = None, lib: Any | None = None) -> None:
        self._download_dir = Path(download_dir) if download_dir else None
        self._lib = lib
        self._sessions: dict[str, AristotleSession] = {}

    def _aristotlelib(self) -> Any:
        if self._lib is None:
            try:
                import aristotlelib
            except ImportError:
                raise RuntimeError(
                    "aristotlelib is not installed. Run: /setup-autoform\n"
                    "Get an API key at: https://aristotle.harmonic.fun/dashboard/keys"
                )
            self._lib = aristotlelib
        return self._lib

    async def submit(
        self,
        session_id: str,
        prompt: str,
        project_dir: str | None = None,
    ) -> dict[str, Any]:
        """Submit a formalization task to Aristotle.

        If the session already has a project, continues it with project.ask().
        Otherwise, creates a new project.
        """
        lib = self._aristotlelib()
        session = self._sessions.get(session_id)

        if session is not None and session.project is not None:
            # Continue existing session
            task = await session.project.ask(prompt)
            session.last_task = task
            session.last_status = _status_value(task.status)
            session.events_seen.clear()
            return {
                "session_id": session_id,
                "project_id": session.project_id,
                "task_id": getattr(task, "agent_task_id", ""),
                "status": session.last_status,
                "mode": "continued",
            }

        # Create new project
        session = AristotleSession(created_at=time.time())

        if project_dir:
            project = await lib.Project.create_from_directory(
                prompt=prompt, project_dir=project_dir
            )
        else:
            project = await lib.Project.create(prompt=prompt)

        session.project = project
        session.project_id = getattr(project, "project_id", "")

        tasks, _ = await project.get_tasks(limit=1, newest_first=True)
        if not tasks:
            raise RuntimeError(f"Aristotle project {session.project_id} created but has no task")

        session.last_task = tasks[0]
        session.last_status = _status_value(tasks[0].status)
        self._sessions[session_id] = session

        return {
            "session_id": session_id,
            "project_id": session.project_id,
            "task_id": getattr(tasks[0], "agent_task_id", ""),
            "status": session.last_status,
            "mode": "created",
        }

    async def poll(self, session_id: str) -> dict[str, Any]:
        """Check the status of a running task. Refreshes from Aristotle's API."""
        session = self._sessions.get(session_id)
        if session is None or session.last_task is None:
            return {"error": f"No active session '{session_id}'"}

        task = session.last_task
        await task.refresh()
        session.last_status = _status_value(task.status)

        result: dict[str, Any] = {
            "session_id": session_id,
            "task_id": getattr(task, "agent_task_id", ""),
            "status": session.last_status,
            "in_flight": session.last_status in _IN_FLIGHT_STATUSES,
            "continuable": session.last_status in _CONTINUABLE_STATUSES,
        }

        # Include summary if task is done
        if session.last_status not in _IN_FLIGHT_STATUSES:
            result["output_summary"] = (getattr(task, "output_summary", None) or "").strip()

        return result

    async def wait(
        self,
        session_id: str,
        poll_interval: int = 15,
        max_wait_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Poll until the task reaches a terminal status, then return the result."""
        session = self._sessions.get(session_id)
        if session is None or session.last_task is None:
            return {"error": f"No active session '{session_id}'"}

        task = session.last_task
        start = time.monotonic()

        while _status_value(task.status) in _IN_FLIGHT_STATUSES:
            if max_wait_seconds and (time.monotonic() - start) > max_wait_seconds:
                return {
                    "session_id": session_id,
                    "status": _status_value(task.status),
                    "timed_out": True,
                    "elapsed_seconds": round(time.monotonic() - start),
                }
            await asyncio.sleep(poll_interval)
            await task.refresh()

        session.last_status = _status_value(task.status)
        summary = (getattr(task, "output_summary", None) or "").strip()

        result: dict[str, Any] = {
            "session_id": session_id,
            "task_id": getattr(task, "agent_task_id", ""),
            "status": session.last_status,
            "output_summary": summary,
            "elapsed_seconds": round(time.monotonic() - start),
        }

        # Download files if configured
        download_note = await self._maybe_download(session)
        if download_note:
            result["downloaded_to"] = str(self._download_dir)

        return result

    async def steer(self, session_id: str, prompt: str) -> dict[str, Any]:
        """Redirect a running Aristotle task with a new prompt.

        Only works while the task is in-flight. Aristotle will incorporate
        the steering prompt into its ongoing work.
        """
        session = self._sessions.get(session_id)
        if session is None or session.project is None:
            return {"error": f"No active session '{session_id}'"}

        task = session.last_task
        if task is not None:
            await task.refresh()
            status = _status_value(task.status)
            if status not in _IN_FLIGHT_STATUSES:
                return {
                    "error": f"Task is not in-flight (status: {status}). Use submit to continue the session instead.",
                    "session_id": session_id,
                }

        try:
            new_task = await session.project.ask(prompt)
        except Exception as err:
            return {"error": f"Steer failed: {err}", "session_id": session_id}

        session.last_task = new_task
        session.last_status = _status_value(new_task.status)
        session.events_seen.clear()

        return {
            "session_id": session_id,
            "task_id": getattr(new_task, "agent_task_id", ""),
            "status": session.last_status,
            "steered": True,
        }

    async def get_events(self, session_id: str, limit: int = 20) -> dict[str, Any]:
        """Fetch recent events from a running task (progress, file edits, etc.)."""
        session = self._sessions.get(session_id)
        if session is None or session.last_task is None:
            return {"error": f"No active session '{session_id}'"}

        try:
            events, _ = await session.last_task.get_events(limit=limit, newest_first=True)
        except Exception as err:
            return {"error": f"Event fetch failed: {err}"}

        formatted = []
        for e in events:
            event_id = getattr(e, "event_id", "")
            is_new = event_id not in session.events_seen
            session.events_seen.add(event_id)
            formatted.append({
                "event_id": event_id,
                "type": getattr(getattr(e, "event_type", None), "name", "unknown"),
                "new": is_new,
            })

        return {
            "session_id": session_id,
            "events": formatted,
        }

    def list_sessions(self) -> dict[str, Any]:
        """List all active sessions with their current status."""
        sessions = []
        for sid, session in self._sessions.items():
            sessions.append({
                "session_id": sid,
                "project_id": session.project_id,
                "status": session.last_status,
                "age_seconds": round(time.time() - session.created_at) if session.created_at else 0,
            })
        return {"sessions": sessions}

    async def _maybe_download(self, session: AristotleSession) -> bool:
        """Download + extract result tarball. Returns True if files were extracted."""
        if self._download_dir is None or session.project is None:
            return False
        try:
            self._download_dir.mkdir(parents=True, exist_ok=True)
            tar_path = self._download_dir / f"{session.project_id}.tar.gz"
            await session.project.get_files(destination=tar_path)
            with tarfile.open(tar_path) as tar:
                tar.extractall(self._download_dir, filter="data")
            return True
        except Exception as err:
            logger.warning("Failed to download Aristotle files: %s", err)
            return False


def create_aristotle_server(manager: AristotleManager) -> FastMCP:
    """Create a FastMCP server for delegating formalization tasks to Aristotle."""
    server = FastMCP(name="autoform-aristotle")

    @server.tool
    def aristotle_submit(
        session_id: str,
        prompt: str,
        project_dir: str = "",
    ) -> str:
        """Submit a formalization task to Aristotle.

        Aristotle is an autonomous formal-reasoning agent. Give it a clear
        task description and it will search Mathlib, write Lean proofs, and
        return finished files. For follow-up turns on the same task, reuse
        the same session_id — Aristotle continues its server-side session.

        Args:
            session_id: Unique identifier for this task (e.g., "thm-2-3" or "convex-sets").
            prompt: The formalization task. Be specific: include the statement,
                    relevant definitions, and which file to write to.
            project_dir: Optional path to a Lean project directory. Aristotle
                         will use it as context (existing code, lakefile, etc.).
        """
        result = asyncio.run(manager.submit(
            session_id=session_id,
            prompt=prompt,
            project_dir=project_dir or None,
        ))
        return json.dumps(result, indent=2)

    @server.tool
    def aristotle_wait(
        session_id: str,
        max_wait_seconds: float = 600,
    ) -> str:
        """Wait for an Aristotle task to complete and return the result.

        Polls until the task reaches a terminal status. Use this after
        aristotle_submit to block until Aristotle finishes.

        Args:
            session_id: The session to wait on.
            max_wait_seconds: Maximum time to wait (default: 10 minutes).
        """
        result = asyncio.run(manager.wait(
            session_id=session_id,
            max_wait_seconds=max_wait_seconds,
        ))
        return json.dumps(result, indent=2)

    @server.tool
    def aristotle_poll(session_id: str) -> str:
        """Check the status of an Aristotle task without blocking.

        Use this for non-blocking status checks (e.g., while doing
        other work in parallel).

        Args:
            session_id: The session to check.
        """
        result = asyncio.run(manager.poll(session_id=session_id))
        return json.dumps(result, indent=2)

    @server.tool
    def aristotle_steer(session_id: str, prompt: str) -> str:
        """Redirect a running Aristotle task with new instructions.

        Only works while the task is in-flight. Use this to correct
        Aristotle's approach or add constraints without restarting.

        Args:
            session_id: The session to steer.
            prompt: New instructions to inject (e.g., "Use Finset.sum_le_sum
                    instead of manual induction").
        """
        result = asyncio.run(manager.steer(session_id=session_id, prompt=prompt))
        return json.dumps(result, indent=2)

    @server.tool
    def aristotle_events(session_id: str, limit: int = 20) -> str:
        """Fetch recent events from a running Aristotle task.

        Shows what Aristotle is doing: proof attempts, file edits,
        Lean builds, etc. Useful for monitoring progress.

        Args:
            session_id: The session to inspect.
            limit: Maximum number of events to return.
        """
        result = asyncio.run(manager.get_events(session_id=session_id, limit=limit))
        return json.dumps(result, indent=2)

    @server.tool
    def aristotle_sessions() -> str:
        """List all active Aristotle sessions with their current status."""
        result = manager.list_sessions()
        return json.dumps(result, indent=2)

    return server


if __name__ == "__main__":
    download_dir = os.environ.get("ARISTOTLE_DOWNLOAD_DIR", "./aristotle-output")
    manager = AristotleManager(download_dir=download_dir)
    server = create_aristotle_server(manager)
    server.run(transport="stdio")
