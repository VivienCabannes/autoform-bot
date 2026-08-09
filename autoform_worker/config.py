"""Worker configuration — plugin-root discovery, project resolution, identity.

The CLI runs from a repo checkout (the plugin model): ``plugin_root()`` finds the
checkout the same way the skills do (env override, else relative to this file)
and ``scripts_modules()`` imports the deterministic control plane from it. The
worker never re-implements what ``scripts/`` already owns.
"""
from __future__ import annotations

import os
import json
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
    import spend_governor  # noqa: PLC0415

    return {
        "backend_config": backend_config,
        "dispatch_queue": dispatch_queue,
        "fslock": fslock,
        "judge_runtime": judge_runtime,
        "recovery_state": recovery_state,
        "review_model": review_model,
        "spend_governor": spend_governor,
    }


def sanitize_worker_id(raw: str) -> str:
    wid = re.sub(r"[^a-z0-9-]+", "-", raw.lower()).strip("-")
    return wid[:64] or "worker"


@dataclass(frozen=True)
class WorkerConfig:
    """Everything a round needs, resolved once at CLI entry."""

    worker_id: str
    project: Path        # dispatch project — the dir owning blueprint/roadmap
    lean_root: Path      # the Lean git repo (may equal project)
    plugin_root: Path
    state_dir: Path      # ~/.autoform/worker/<wid>/
    respect_claims: bool = True

    @property
    def blueprint_path(self) -> Path:
        blueprint = self.project / "blueprint"
        if (blueprint / "roadmap").is_dir():
            return blueprint
        # Read-only migration compatibility for projects created before the
        # Markdown authority. Setup never creates this form.
        return self.project / "graph.json"

    @property
    def graph_path(self) -> Path:
        """Deprecated alias for legacy integrations; new projects use blueprint_path."""
        return self.blueprint_path

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


def _find_blueprint_project(explicit: Path | None) -> Path:
    """Resolve the project containing the authoritative Markdown roadmap."""
    if explicit is not None:
        proj = explicit.expanduser().resolve()
        if not (proj / "blueprint" / "roadmap").is_dir() and not (proj / "graph.json").is_file():
            raise Die(f"no blueprint/roadmap in {proj} — run /autoform:setup first")
        return proj
    env = os.environ.get("AUTOFORM_DISPATCH_PROJECT")
    if env:
        proj = Path(env).expanduser().resolve()
        if not (proj / "blueprint" / "roadmap").is_dir() and not (proj / "graph.json").is_file():
            raise Die(f"AUTOFORM_DISPATCH_PROJECT={env} has no blueprint/roadmap")
        return proj
    cwd = Path.cwd()
    for cand in (cwd, *cwd.parents):
        if (cand / "blueprint" / "roadmap").is_dir() or (cand / "graph.json").is_file():
            return cand.resolve()
    raise Die(
        "no dispatch project found — pass --project, set AUTOFORM_DISPATCH_PROJECT, "
        "or run from a repository containing blueprint/roadmap"
    )


def _lean_root_of(project: Path) -> Path:
    """Return the Lean checkout; projects are self-contained by default."""
    configured = os.environ.get("AUTOFORM_LEAN_ROOT")
    if configured:
        path = Path(configured).expanduser().resolve()
        if not path.is_dir():
            raise Die(f"AUTOFORM_LEAN_ROOT is not a directory: {path}")
        return path
    legacy = project / "graph.json"
    if legacy.is_file() and not (project / "blueprint" / "roadmap").is_dir():
        try:
            value = json.loads(legacy.read_text(encoding="utf-8")).get("metadata", {}).get("lean_root")
            if value and Path(value).is_dir():
                return Path(value).resolve()
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    return project


def resolve_config(
    project: Path | None = None,
    worker_id: str | None = None,
    respect_claims: bool = True,
) -> WorkerConfig:
    proj = _find_blueprint_project(project)
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
