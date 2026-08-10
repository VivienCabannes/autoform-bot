"""Worker configuration for a Markdown-authoritative Autoform project."""
from __future__ import annotations

import os
import re
import socket
import sys
from dataclasses import dataclass
from pathlib import Path

from autoform_cli.runtime import RuntimeGraph, RuntimeProjectionError, load_runtime_graph

from .errors import Die

_ROOT_ENVS = ("AUTOFORM_PLUGIN_ROOT", "MUSE_PLUGIN_ROOT", "PLUGIN_ROOT", "CLAUDE_PLUGIN_ROOT")


def plugin_root() -> Path:
    """Return the Autoform checkout containing the worker control plane."""
    candidates = [Path(v) for v in (os.environ.get(e) for e in _ROOT_ENVS) if v]
    candidates.append(Path(__file__).resolve().parents[1])
    for candidate in candidates:
        if (candidate / "scripts").is_dir() and (candidate / "internal").is_dir():
            return candidate.resolve()
    raise Die(
        "cannot locate the autoform plugin root (a dir containing scripts/ and internal/); "
        "set AUTOFORM_PLUGIN_ROOT or run from a repo checkout"
    )


def scripts_modules():
    """Import the mature private worker-control modules from the checkout."""
    root = plugin_root()
    for path in (root, root / "scripts", root / "scripts" / "review_ui"):
        value = str(path)
        if value not in sys.path:
            sys.path.insert(0, value)
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
    worker_id = re.sub(r"[^a-z0-9-]+", "-", raw.lower()).strip("-")
    return worker_id[:64] or "worker"


_OFF = frozenset({"0", "no", "off", "false"})


def _env_flag(name: str, default: bool = True) -> bool:
    """Read an opt-out environment switch; anything unset keeps the default."""
    raw = os.environ.get(name)
    return default if raw is None else raw.strip().casefold() not in _OFF


@dataclass(frozen=True)
class WorkerConfig:
    """Everything a worker command needs, resolved once at CLI entry."""

    worker_id: str
    project: Path
    lean_root: Path
    plugin_root: Path
    state_dir: Path
    runtime: RuntimeGraph
    respect_claims: bool = True

    #: Article IDs are path-derived, so moving an article changes its ID and
    #: orphans durable state keyed by the old one. The round reconciles that by
    #: parking tasks whose node has vanished rather than letting them reference
    #: nothing, which is why stateful execution is on by default. Set
    #: ``AUTOFORM_DURABLE_IDENTITY=0`` to go back to refusing stateful work.
    durable_identity_ready: bool = True

    #: Unattended formalization is the default mode: an agent that finds a node's
    #: Lean statement does not match its source repairs the article rather than
    #: parking it for a person. Set ``AUTOFORM_STATEMENT_REPAIR=0`` to require a
    #: human for every statement change.
    statement_repair: bool = True

    @property
    def compatibility_graph_path(self) -> Path:
        """Disposable private snapshot for legacy prover adapters."""
        from .runtime_graph import write_private_snapshot

        return write_private_snapshot(
            self.runtime,
            self.state_dir / "runtime-compat-v1.json",
            self.lean_root,
        )

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


def _has_roadmap(path: Path) -> bool:
    return (path / "blueprint" / "roadmap").is_dir()


def _find_project(explicit: Path | None) -> Path:
    """Resolve a project containing ``blueprint/roadmap``; never inspect graph.json."""
    if explicit is not None:
        project = explicit.expanduser().resolve()
        if not _has_roadmap(project):
            raise Die(f"no blueprint/roadmap in {project} - run /autoform:setup first")
        return project
    env = os.environ.get("AUTOFORM_DISPATCH_PROJECT")
    if env:
        project = Path(env).expanduser().resolve()
        if not _has_roadmap(project):
            raise Die(f"AUTOFORM_DISPATCH_PROJECT={env} has no blueprint/roadmap")
        return project
    cwd = Path.cwd().resolve()
    for candidate in (cwd, *cwd.parents):
        if _has_roadmap(candidate):
            return candidate
    raise Die(
        "no Autoform project found - pass --project, set AUTOFORM_DISPATCH_PROJECT, "
        "or run inside a project containing blueprint/roadmap"
    )


def _lean_root_of(project: Path, explicit: Path | None) -> Path:
    if explicit is not None:
        root = explicit.expanduser().resolve()
        if not root.is_dir():
            raise Die(f"Lean root does not exist: {root}")
        return root
    env = os.environ.get("AUTOFORM_LEAN_ROOT")
    if env:
        root = Path(env).expanduser().resolve()
        if not root.is_dir():
            raise Die(f"AUTOFORM_LEAN_ROOT={env} is not a directory")
        return root
    return project


def resolve_config(
    project: Path | None = None,
    worker_id: str | None = None,
    respect_claims: bool = True,
    lean_root: Path | None = None,
) -> WorkerConfig:
    """Resolve a worker configuration and its immutable runtime snapshot."""
    resolved_project = _find_project(project)
    resolved_lean = _lean_root_of(resolved_project, lean_root)
    try:
        runtime = load_runtime_graph(resolved_project, lean_root=resolved_lean)
    except RuntimeProjectionError as error:
        raise Die(f"invalid Markdown runtime: {error}") from error
    raw_worker_id = worker_id or os.environ.get("AUTOFORM_WORKER_ID") or ""
    if not raw_worker_id:
        raw_worker_id = f"{os.environ.get('USER', 'worker')}-{socket.gethostname().split('.')[0]}"
    resolved_worker_id = sanitize_worker_id(raw_worker_id)
    state_root = Path(
        os.environ.get("AUTOFORM_WORKER_STATE", str(Path.home() / ".autoform" / "worker"))
    )
    state_dir = state_root / resolved_worker_id
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "logs").mkdir(exist_ok=True)
    return WorkerConfig(
        worker_id=resolved_worker_id,
        project=resolved_project,
        lean_root=resolved_lean,
        plugin_root=plugin_root(),
        state_dir=state_dir,
        runtime=runtime,
        respect_claims=respect_claims,
        durable_identity_ready=_env_flag("AUTOFORM_DURABLE_IDENTITY"),
        statement_repair=_env_flag("AUTOFORM_STATEMENT_REPAIR"),
    )
