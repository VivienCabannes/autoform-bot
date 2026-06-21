"""Aristotle (Harmonic) backend — session management + node delegation.

Aristotle is **not** a chat LLM. It is an autonomous formal-reasoning agent that
takes a prompt (and optionally a whole Lean project directory), runs its own
internal tools (proof search, ``lake`` builds, file edits), and returns finished
Lean files plus a natural-language ``output_summary``. We map that job-based API
onto two surfaces:

* :class:`AristotleManager` — a thin, stateful wrapper over one
  ``aristotlelib.Project`` per ``session_id``: submit / poll / wait / steer /
  events / list. This is what the MCP tools call. Ported from the proven
  integration in ``core/inference/sdk/aristotle.py`` and
  ``examples/servers/aristotle/server.py``.

* :func:`delegate_to_node` — the **prover-backend entry**. It is the C-side
  implementation of the one swappable interface the design pins down:

      (target node + spec) -> proof written back to the node

  Given a plan's ``graph.json`` and a target node ``id``, it reads that node's
  *spec* (its informal statement + ``source_refs`` + ``mathlib_declarations`` +
  in-tier ``depends_on``), hands the whole Lean project to Aristotle, polls to a
  terminal status, lands the returned Lean files into the project, and writes the
  proof back to the node (Lean files in the project + the node's prose file).

HARD CONSTRAINT (see SHARED_SPEC / design doc): Aristotle ONLY produces a proof
*into a node*. It does **not** review, score, taint, or touch the sidecar — the
proof it lands feeds the SAME incremental jury -> ``review_status.json`` -> review
surface that the in-session worker feeds (built by PRs A/E). Nothing here reads or
writes ``review_status.json`` or runs any verdict logic.

``aristotlelib`` is an **opt-in** dependency (the ``aristotle`` extra). It is
imported lazily, so this module imports cleanly without it installed.

Honest limitations (inherited from the backend):

* Aristotle is job-based and slow (minutes to hours); prefer one delegation at a
  time over racing many.
* It bills by compute on Harmonic's side, not tokens. The API itself is **free**
  (no metered cost to the user) but needs ``ARISTOTLE_API_KEY`` + network, so it
  is OPT-IN and default-off.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import tarfile
import time
from dataclasses import dataclass, field
from logging import getLogger
from pathlib import Path
from typing import Any

logger = getLogger(__name__)

# ---------------------------------------------------------------------------
# Status classification (compared by string value, enum-agnostic)
# ---------------------------------------------------------------------------

# Still running on Aristotle's side; keep polling.
_IN_FLIGHT_STATUSES = frozenset({"QUEUED", "IN_PROGRESS"})
# Terminal but resumable: the server-side session is preserved, so the next turn
# should *continue* via ``project.ask`` rather than resubmit.
_CONTINUABLE_STATUSES = frozenset({"COMPLETE_WITH_ERRORS", "OUT_OF_BUDGET"})

# Top-level dirs never copied back from Aristotle's returned project.
_SKIP_TOP = {".git", ".lake"}


def _status_value(status: Any) -> str:
    """Return the ``.value`` of a ``TaskStatus`` (or the string itself)."""
    return str(getattr(status, "value", status))


@dataclass
class AristotleSession:
    """One Aristotle project session (a single ``aristotlelib.Project``)."""

    project: Any = None
    project_id: str = ""
    last_task: Any = None
    last_status: str = ""
    created_at: float = 0.0
    events_seen: set[str] = field(default_factory=set)


class AristotleManager:
    """Manage multiple Aristotle sessions for concurrent delegation.

    Each session is keyed by a user-chosen ``session_id`` (e.g. a node ``id`` or
    theorem name). Sessions are independent.

    Args:
        download_dir: When set, completed tasks' result tarballs are downloaded
            and extracted here.
        lib: The ``aristotlelib`` module (injected for testing). When ``None`` it
            is imported lazily on first use — so this class is constructible and
            importable without the optional dependency installed.
    """

    def __init__(self, *, download_dir: str | None = None, lib: Any | None = None) -> None:
        self._download_dir = Path(download_dir) if download_dir else None
        self._lib = lib
        self._sessions: dict[str, AristotleSession] = {}

    def _aristotlelib(self) -> Any:
        if self._lib is None:
            try:
                import aristotlelib  # lazy: only required when Aristotle is actually used
            except ImportError as err:  # pragma: no cover - exercised only without the extra
                raise RuntimeError(
                    "aristotlelib is not installed. Install the opt-in extra:\n"
                    "    uv sync --extra aristotle\n"
                    "and set ARISTOTLE_API_KEY (free key at "
                    "https://aristotle.harmonic.fun/dashboard/keys)."
                ) from err
            # API guard: this client targets the aristotlelib >=2.0 API
            # (``Project.create_from_directory`` / ``ask`` / ``get_tasks``). An older
            # lib (e.g. 0.5.x) has an incompatible API and would fail with a cryptic
            # AttributeError deep in submit() — surface it clearly instead. This fires
            # only when running outside the plugin's locked env (pyproject pins >=2.0).
            proj = getattr(aristotlelib, "Project", None)
            if proj is None or not hasattr(proj, "create_from_directory"):
                try:
                    import importlib.metadata as _md
                    ver = _md.version("aristotlelib")
                except Exception:
                    ver = "unknown"
                raise RuntimeError(
                    f"aristotlelib {ver} has an incompatible API — this client needs the "
                    ">=2.0 API (Project.create_from_directory / ask / get_tasks). You are "
                    "likely running outside the plugin's locked env. Run the Aristotle "
                    "backend via the prover MCP server (`uv run --extra aristotle python -m "
                    "servers.prover.server`) or `uv sync --extra aristotle` (the pyproject "
                    "pins aristotlelib>=2.0), not a stray global install."
                )
            self._lib = aristotlelib
        return self._lib

    # ------------------------------------------------------------------
    # Submit / poll / wait / steer / events / list
    # ------------------------------------------------------------------

    async def submit(
        self,
        session_id: str,
        prompt: str,
        project_dir: str | None = None,
    ) -> dict[str, Any]:
        """Submit a formalization task.

        If the session already has a project, continue it with ``project.ask``;
        otherwise create a new project (bundling ``project_dir`` when given).
        """
        lib = self._aristotlelib()
        session = self._sessions.get(session_id)

        if session is not None and session.project is not None:
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

        session = AristotleSession(created_at=time.time())
        if project_dir:
            project = await lib.Project.create_from_directory(prompt=prompt, project_dir=project_dir)
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
        """Non-blocking status check (refreshes from Aristotle's API)."""
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
        if await self._maybe_download(session):
            result["downloaded_to"] = str(self._download_dir)
        return result

    async def steer(self, session_id: str, prompt: str) -> dict[str, Any]:
        """Redirect a running task with a new prompt (in-flight only)."""
        session = self._sessions.get(session_id)
        if session is None or session.project is None:
            return {"error": f"No active session '{session_id}'"}

        task = session.last_task
        if task is not None:
            await task.refresh()
            status = _status_value(task.status)
            if status not in _IN_FLIGHT_STATUSES:
                return {
                    "error": (
                        f"Task is not in-flight (status: {status}). "
                        "Use submit to continue the session instead."
                    ),
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
        return {"session_id": session_id, "events": formatted}

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

    # ------------------------------------------------------------------
    # Result download / extraction
    # ------------------------------------------------------------------

    async def _maybe_download(self, session: AristotleSession) -> bool:
        """Download + extract the result tarball to ``download_dir``. Best effort."""
        if self._download_dir is None or session.project is None:
            return False
        try:
            self._download_dir.mkdir(parents=True, exist_ok=True)
            tar_path = self._download_dir / f"{session.project_id}.tar.gz"
            await session.project.get_files(destination=tar_path)
            _safe_extract(tar_path, self._download_dir)
            return True
        except Exception as err:
            logger.warning("Failed to download Aristotle files: %s", err)
            return False

    async def download_result(
        self,
        session_id: str,
        dest_dir: str | Path,
        *,
        retries: int = 3,
        retry_delay: float = 5.0,
    ) -> Path | None:
        """Download + extract a session's result, returning the extracted
        project-root directory (or ``None`` if unavailable after ``retries``).

        Unlike the best-effort :meth:`_maybe_download` side channel, this is
        explicit and **retries**: when a task first reaches a terminal status the
        files can be momentarily unavailable, so a single ``get_files`` can
        transiently fail. Callers that must land the output (the node-delegation
        path) use this and treat ``None`` as a hard failure.
        """
        session = self._sessions.get(session_id)
        if session is None or session.project is None:
            return None
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        last_err: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                await session.project.refresh()
                tar_path = dest / f"{session.project_id or 'aristotle'}.tar.gz"
                await session.project.get_files(destination=tar_path)
                _safe_extract(tar_path, dest)
                roots = [p for p in dest.iterdir() if p.is_dir()]
                if roots:
                    return next((p for p in roots if p.name.endswith("_aristotle")), roots[0])
                last_err = RuntimeError("tarball extracted no project directory")
            except Exception as err:
                last_err = err
                logger.warning("download_result attempt %d/%d failed: %s", attempt, retries, err)
            if attempt < retries:
                await asyncio.sleep(retry_delay)
        logger.error("download_result exhausted %d retries: %s", retries, last_err)
        return None


def _safe_extract(tar_path: Path, dest: Path) -> None:
    """Extract a tarball, using the ``data`` filter when available (py>=3.12)."""
    with tarfile.open(tar_path) as tar:
        try:
            tar.extractall(dest, filter="data")  # type: ignore[arg-type]
        except TypeError:  # pragma: no cover - older Pythons lack the filter kwarg
            tar.extractall(dest)  # noqa: S202 - trusted Aristotle output


# ===========================================================================
# Prover-backend entry: (target node + spec) -> proof written back to the node
# ===========================================================================

# The default goal Aristotle is held to. The reviewer/packet path (PR A/E) does
# the verification; here we only state the producer's no-cheating contract so the
# prover does not deliver a sorry'd file as done.
DEFAULT_DELEGATE_SYSTEM = (
    "You are an expert Lean 4 / Mathlib formalizer working inside an existing Lean "
    "project. Formalize the requested target into the project, writing idiomatic, "
    "compiling Lean 4. Do NOT use `sorry`, `admit`, or introduce new axioms; the "
    "claimed statement must be genuinely proved. Make the project build with `lake build`."
)


def _slugify(node_id: str) -> str:
    """Mirror the exporter's slug rule for ``informal_content/<slug>.md``."""
    slug = re.sub(r"[^a-z0-9]+", "-", node_id.lower()).strip("-")
    return slug or "node"


def load_node(graph_path: Path, node_id: str) -> dict[str, Any]:
    """Read a node record from ``graph.json``. Raises ``KeyError`` if absent."""
    graph = json.loads(Path(graph_path).read_text(encoding="utf-8"))
    nodes = graph.get("nodes", {})
    if node_id not in nodes:
        raise KeyError(f"node {node_id!r} not found in {graph_path}")
    return nodes[node_id]


def build_node_spec(
    graph_path: Path,
    node_id: str,
    *,
    project_dir: Path,
) -> str:
    """Assemble the *spec prompt* Aristotle is given for a target node.

    The spec is read off the node itself (the design's "the node IS the informal
    statement"): the prose statement from ``informal_content/<id>.md`` plus the
    structural hints a prover needs — kind, ``source_refs`` (faithfulness
    anchor), ``mathlib_declarations`` (the decl to realize), ``mathlib_file``,
    and the in-tier ``depends_on`` it may rely on.
    """
    node = load_node(graph_path, node_id)
    project_dir = Path(project_dir)

    statement = ""
    content_rel = node.get("content")
    if content_rel:
        prose_path = project_dir / content_rel
        if prose_path.exists():
            statement = prose_path.read_text(encoding="utf-8").strip()

    lines: list[str] = [f"# Formalization target: {node_id}", ""]
    kind = node.get("kind")
    if kind:
        lines.append(f"Kind: {kind}")
    decls = node.get("mathlib_declarations") or []
    if decls:
        lines.append(f"Realize the Mathlib declaration(s): {', '.join(decls)}")
    if node.get("mathlib_file"):
        lines.append(f"Primary Mathlib file: {node['mathlib_file']}")
    if node.get("mathlib_notes"):
        lines.append(f"Mathlib notes: {node['mathlib_notes']}")
    deps = node.get("depends_on") or []
    if deps:
        lines.append(f"You may rely on these already-stated prerequisites: {', '.join(deps)}")

    refs = node.get("source_refs") or []
    if refs:
        lines.append("")
        lines.append("Source references (state faithfully against these):")
        for r in refs:
            loc = r.get("location", "")
            f = r.get("file", "")
            lines.append(f"  - {f}: {loc}")

    lines.append("")
    if statement:
        lines.append("## Statement (and proof, if present) to formalize")
        lines.append(statement)
    else:
        lines.append(
            "## Statement\n(no prose file yet — formalize the target described above, "
            "stating it faithfully against the source references)"
        )
    return "\n".join(lines)


def _overlay_project(root: Path, project_dir: Path) -> int:
    """Copy Aristotle's returned files (under ``root``) over ``project_dir`` at the
    same relative paths. Skips ``.git``/``.lake``, dirs, and symlinks. Returns the
    number of files copied."""
    copied = 0
    for src in root.rglob("*"):
        if src.is_dir() or src.is_symlink():
            continue
        rel = src.relative_to(root)
        if rel.parts and rel.parts[0] in _SKIP_TOP:
            continue
        dest = project_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        copied += 1
    return copied


def _record_proof_in_prose(
    graph_path: Path,
    node_id: str,
    node: dict[str, Any],
    project_dir: Path,
    summary: str,
) -> str | None:
    """Ensure the node's prose file exists and append Aristotle's proof summary.

    Returns the ``content`` path (relative) that should be merged onto the node,
    or ``None`` if no prose file could be established. Does NOT write graph.json
    (that goes through ``merge_node.py`` — see :func:`merge_payload`).
    """
    content_rel = node.get("content") or f"informal_content/{_slugify(node_id)}.md"
    prose_path = project_dir / content_rel
    prose_path.parent.mkdir(parents=True, exist_ok=True)

    existing = prose_path.read_text(encoding="utf-8") if prose_path.exists() else f"# {node_id}\n"
    block = (
        "\n\n## Proof (delegated to Aristotle)\n\n"
        f"{summary.strip() or '(Aristotle returned no summary; see the landed Lean files.)'}\n"
    )
    # Idempotent-ish: don't stack duplicate Aristotle blocks on re-runs.
    marker = "## Proof (delegated to Aristotle)"
    if marker in existing:
        head = existing.split(marker, 1)[0].rstrip()
        existing = head + "\n"
    prose_path.write_text(existing.rstrip() + block, encoding="utf-8")
    return content_rel


def merge_payload(node_id: str, node: dict[str, Any], content_rel: str | None) -> dict[str, Any]:
    """Build the single-node ``merge_node.py`` payload that records the proof.

    The ONLY structural change a backend makes is linking the node to its prose
    file (``content``) once a proof exists. Everything else about the node is
    left untouched, and review/verdict state is never touched here.
    """
    rec = dict(node)
    if content_rel:
        rec["content"] = content_rel
    return {"upsert": {node_id: rec}}


@dataclass
class DelegationResult:
    """Outcome of :func:`delegate_to_node`."""

    node_id: str
    status: str
    landed_files: int
    content: str | None
    output_summary: str
    merge_payload: dict[str, Any]
    project_id: str = ""

    @property
    def ok(self) -> bool:
        """True when Aristotle reached a non-error terminal status and landed files."""
        return self.status in ("COMPLETE", "COMPLETE_WITH_ERRORS") and self.landed_files > 0


async def delegate_to_node(
    *,
    graph_path: str | Path,
    node_id: str,
    project_dir: str | Path,
    manager: AristotleManager | None = None,
    system_prompt: str = DEFAULT_DELEGATE_SYSTEM,
    extra_instructions: str = "",
    poll_interval: int = 20,
    max_wait_seconds: float | None = 5400,
) -> DelegationResult:
    """The prover-backend interface: ``(target node + spec) -> proof into the node``.

    1. Build the node's spec prompt from ``graph.json`` + its prose/refs.
    2. Submit the whole Lean ``project_dir`` to Aristotle and poll to terminal.
    3. Land the returned Lean files back into ``project_dir``.
    4. Record the proof summary in the node's prose file and return a
       ``merge_node.py`` payload that links ``content`` (the caller applies it
       through the single locked writer).

    This does NOT review, score, or touch ``review_status.json`` — the landed
    proof feeds the SAME jury/sidecar/review pipeline as the in-session worker
    (PRs A/E). Aristotle is the backend; the review surface is built elsewhere.
    """
    graph_path = Path(graph_path)
    project_dir = Path(project_dir)
    node = load_node(graph_path, node_id)

    mgr = manager or AristotleManager(download_dir=str(project_dir / ".aristotle-cache"))

    spec = build_node_spec(graph_path, node_id, project_dir=project_dir)
    prompt = f"{system_prompt}\n\n{spec}"
    if extra_instructions:
        prompt += f"\n\n## Additional instructions\n{extra_instructions}"

    await mgr.submit(session_id=node_id, prompt=prompt, project_dir=str(project_dir))
    waited = await mgr.wait(
        session_id=node_id,
        poll_interval=poll_interval,
        max_wait_seconds=max_wait_seconds,
    )
    status = str(waited.get("status", "UNKNOWN"))
    summary = str(waited.get("output_summary", ""))

    landed = 0
    content_rel: str | None = node.get("content")
    with _temp_dir() as td:
        root = await mgr.download_result(node_id, td)
        if root is not None:
            landed = _overlay_project(root, project_dir)
    if landed:
        content_rel = _record_proof_in_prose(graph_path, node_id, node, project_dir, summary)
    else:
        logger.warning("Aristotle delegation for %r landed no files (status=%s)", node_id, status)

    session = mgr._sessions.get(node_id)
    project_id = session.project_id if session else ""

    return DelegationResult(
        node_id=node_id,
        status=status,
        landed_files=landed,
        content=content_rel,
        output_summary=summary,
        merge_payload=merge_payload(node_id, node, content_rel if landed else None),
        project_id=project_id,
    )


class _temp_dir:
    """Context manager yielding a fresh temp directory path (str)."""

    def __enter__(self) -> str:
        import tempfile

        self._td = tempfile.TemporaryDirectory()
        return self._td.name

    def __exit__(self, *exc: Any) -> bool:
        self._td.cleanup()
        return False
