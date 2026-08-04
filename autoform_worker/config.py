"""Worker configuration — plugin-root discovery, project resolution, identity.

The CLI runs from a repo checkout (the plugin model): ``plugin_root()`` finds the
checkout the same way the skills do (env override, else relative to this file)
and ``scripts_modules()`` imports the deterministic control plane from it. The
worker never re-implements what ``scripts/`` already owns.
"""
from __future__ import annotations

import json
import os
import re
import socket
import sys
from dataclasses import dataclass
from pathlib import Path

from .errors import Die

_ROOT_ENVS = ("AUTOFORM_PLUGIN_ROOT", "MUSE_PLUGIN_ROOT", "PLUGIN_ROOT", "CLAUDE_PLUGIN_ROOT")


def plugin_root() -> Path:
    """The autoform-bot checkout root (contains ``scripts/`` + ``internal/``).

    Resolution mirrors the skills' ritual: a valid env override wins, else the
    package's parent directory. Fails closed with a clear message — a wheel
    install without a checkout cannot run worker rounds.
    """
    candidates = [Path(v) for v in (os.environ.get(e) for e in _ROOT_ENVS) if v]
    candidates.append(Path(__file__).resolve().parents[1])
    for cand in candidates:
        if (cand / "scripts").is_dir() and (cand / "internal").is_dir():
            return cand.resolve()
    raise Die(
        "cannot locate the autoform plugin root (a dir containing scripts/ and internal/); "
        "set AUTOFORM_PLUGIN_ROOT or run from a repo checkout"
    )


def scripts_modules():
    """Import and return the shared scripts modules as a namespace dict.

    Path setup matches ``scripts/dispatch_runner.py``: the flat ``scripts/`` dir
    plus ``scripts/review_ui`` (fslock/review_model), plus the root for
    ``servers.*``.
    """
    root = plugin_root()
    for p in (root, root / "scripts", root / "scripts" / "review_ui"):
        sp = str(p)
        if sp not in sys.path:
            sys.path.insert(0, sp)
    import backend_config  # noqa: PLC0415
    import dispatch_queue  # noqa: PLC0415
    import fslock  # noqa: PLC0415
    import judge_runtime  # noqa: PLC0415
    import recovery_state  # noqa: PLC0415
    import review_model  # noqa: PLC0415

    return {
        "backend_config": backend_config,
        "dispatch_queue": dispatch_queue,
        "fslock": fslock,
        "judge_runtime": judge_runtime,
        "recovery_state": recovery_state,
        "review_model": review_model,
    }


def sanitize_worker_id(raw: str) -> str:
    wid = re.sub(r"[^a-z0-9-]+", "-", raw.lower()).strip("-")
    return wid[:64] or "worker"


@dataclass(frozen=True)
class WorkerConfig:
    """Everything a round needs, resolved once at CLI entry."""

    worker_id: str
    project: Path        # dispatch project — the dir owning graph.json
    lean_root: Path      # the Lean git repo (may equal project)
    plugin_root: Path
    state_dir: Path      # ~/.autoform/worker/<wid>/
    respect_claims: bool = True

    @property
    def graph_path(self) -> Path:
        return self.project / "graph.json"

    @property
    def counters_path(self) -> Path:
        return self.state_dir / "counters.json"

    @property
    def folded_path(self) -> Path:
        return self.state_dir / "folded.json"

    @property
    def log_dir(self) -> Path:
        return self.state_dir / "logs"

    @property
    def claims_scratch(self) -> Path:
        return self.state_dir / "claims-scratch"


def _find_graph_project(explicit: Path | None) -> Path:
    """The dispatch project: explicit arg, env, else the cwd if it owns a graph."""
    if explicit is not None:
        proj = explicit.expanduser().resolve()
        if not (proj / "graph.json").exists():
            raise Die(f"no graph.json in {proj} — run /autoform:setup first (or pass the right --project)")
        return proj
    env = os.environ.get("AUTOFORM_DISPATCH_PROJECT")
    if env:
        proj = Path(env).expanduser().resolve()
        if not (proj / "graph.json").exists():
            raise Die(f"AUTOFORM_DISPATCH_PROJECT={env} has no graph.json")
        return proj
    cwd = Path.cwd()
    for cand in (cwd, cwd / ".autoform"):
        if (cand / "graph.json").exists():
            return cand.resolve()
    raise Die(
        "no dispatch project found — pass --project, set AUTOFORM_DISPATCH_PROJECT, "
        "or run from a directory containing graph.json"
    )


def _lean_root_of(project: Path) -> Path:
    """The Lean repo root from graph metadata (the durable pointer), else the project."""
    try:
        meta = json.loads((project / "graph.json").read_text(encoding="utf-8")).get("metadata", {})
    except (OSError, json.JSONDecodeError) as error:
        raise Die(f"cannot read {project / 'graph.json'}: {error}") from error
    lean_root = meta.get("lean_root")
    if lean_root:
        p = Path(lean_root)
        if p.is_dir():
            return p.resolve()
    return project


def resolve_config(
    project: Path | None = None,
    worker_id: str | None = None,
    respect_claims: bool = True,
) -> WorkerConfig:
    proj = _find_graph_project(project)
    lean_root = _lean_root_of(proj)
    wid_raw = worker_id or os.environ.get("AUTOFORM_WORKER_ID") or ""
    if not wid_raw:
        wid_raw = f"{os.environ.get('USER', 'worker')}-{socket.gethostname().split('.')[0]}"
    wid = sanitize_worker_id(wid_raw)
    state_root = Path(os.environ.get("AUTOFORM_WORKER_STATE", str(Path.home() / ".autoform" / "worker")))
    state_dir = state_root / wid
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "logs").mkdir(exist_ok=True)
    if os.environ.get("AUTOFORM_RESPECT_CLAIMS", "").lower() in {"0", "false", "no"}:
        respect_claims = False
    return WorkerConfig(
        worker_id=wid,
        project=proj,
        lean_root=lean_root,
        plugin_root=plugin_root(),
        state_dir=state_dir,
        respect_claims=respect_claims,
    )
