"""The agent role registry — the system's extension point.

An agent role is just a Markdown file with frontmatter. Dropping a new
``agents/<role>.md`` into the plugin — or a project-local
``<project>/.autoform/agents/<role>.md`` — makes a new agent type available
*everywhere*: the dashboard's drag palette, the durable queue's accepted kinds,
and the worker loop's stage cascade. No Python edit, no hardcoded list.

Frontmatter contract (``name`` and ``description`` are required and already
enforced by ``scripts/lint_plugin.py``; everything below is optional, and hosts
that don't understand a key ignore it)::

    ---
    name: counterexample-hunter
    description: >
      Tries to REFUTE a node's statement before anyone spends compute proving it.
    kind: counterexample        # queue kind; default: the file stem
    label: Counterexample       # dashboard palette label; default: title-cased name
    icon: ⚂                     # dashboard palette icon
    blurb: hunt a counterexample here     # palette tooltip; default: description head
    applies: any                # any | tier1 | tier2 — where it may be dropped
    drained_by: agent           # agent (host CLI) | engine (dispatch_runner) | none
    writes: graph               # none | content | graph — what it may durably change
    ---

``drained_by`` decides who runs it. ``engine`` is reserved for the two
deterministic kinds the dispatcher owns (``reviewer``/``worker``); everything
else defaults to ``agent`` — spawned by the worker CLI (or an interactive host)
with the role's own Markdown body as its instructions.

This module is stdlib-only and importable from ``scripts/`` without the worker
package installed, because the dashboard and the queue both consume it.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

_FM_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)

#: Kinds the deterministic engine drains; never spawned as host-CLI roles.
ENGINE_KINDS = ("reviewer", "worker")

#: Roles that exist as prompts but are invoked *inside* another role's pipeline
#: rather than dropped on the queue themselves.
_NON_QUEUE_ROLES = frozenset({
    "splitter", "autoform-reader", "autoform-worker", "source-searcher",
    "proof-strategy-researcher",
})


@dataclass(frozen=True)
class AgentRole:
    """One dispatchable role, discovered from a Markdown file."""

    name: str
    kind: str
    description: str
    path: Path
    label: str = ""
    icon: str = "◆"
    blurb: str = ""
    applies: str = "any"
    drained_by: str = "agent"
    writes: str = "none"
    source: str = "plugin"       # plugin | project
    queueable: bool = True

    def body(self) -> str:
        """The role's instructions — the Markdown after the frontmatter.

        An unreadable or mis-encoded file yields an empty body rather than
        raising: a role that cannot be read must not take down the round that
        merely listed it.
        """
        try:
            text = self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return ""
        match = _FM_RE.match(text)
        return text[match.end():] if match else text

    def to_palette_entry(self) -> dict:
        return {
            "id": self.kind,
            "label": self.label or self.name.replace("-", " ").capitalize(),
            "icon": self.icon,
            "blurb": self.blurb or " ".join(self.description.split())[:120],
            "applies": self.applies,
            "source": self.source,
        }


def _parse_frontmatter(text: str) -> dict:
    """Minimal YAML-subset frontmatter parse: ``key: value`` plus ``>``/``|``
    folded blocks. Deliberately dependency-free — this runs inside the
    stdlib-only dashboard and lint paths."""
    match = _FM_RE.match(text)
    if not match:
        return {}
    data: dict = {}
    key: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        if key is not None:
            data[key] = " ".join(" ".join(buffer).split())

    for line in match.group(1).splitlines():
        if not line.strip():
            continue
        if line[:1].isspace() and key is not None:
            buffer.append(line.strip())
            continue
        flush()
        head, _, value = line.partition(":")
        key, buffer = head.strip(), []
        value = value.strip()
        if value and value not in {">", "|", ">-", "|-"}:
            buffer.append(value)
    flush()
    return data


def _role_dirs(plugin_root: Path, project: Path | None) -> list[tuple[Path, str]]:
    dirs = [(plugin_root / "agents", "plugin")]
    if project is not None:
        dirs.append((project / ".autoform" / "agents", "project"))
    extra = os.environ.get("AUTOFORM_AGENT_PATH", "")
    dirs += [(Path(p), "project") for p in extra.split(os.pathsep) if p.strip()]
    return dirs


def discover(plugin_root: Path, project: Path | None = None) -> dict[str, AgentRole]:
    """All dispatchable roles, keyed by queue kind.

    Project-local roles override plugin roles of the same kind, so a project can
    specialize (e.g. a stricter `holistic`) without forking the plugin.
    """
    roles: dict[str, AgentRole] = {}
    for directory, source in _role_dirs(plugin_root, project):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.md")):
            try:
                fm = _parse_frontmatter(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError):
                continue      # a single bad role file must never sink the registry
            name = str(fm.get("name") or path.stem)
            kind = str(fm.get("kind") or path.stem)
            explicit_queue = "kind" in fm or "drained_by" in fm
            queueable = explicit_queue or name not in _NON_QUEUE_ROLES
            drained = str(fm.get("drained_by") or
                          ("engine" if kind in ENGINE_KINDS else "agent"))
            if drained == "none":
                # An explicit opt-out. Scanned after the plugin dirs, so a
                # project file can also *disable* a shipped role this way.
                roles.pop(kind, None)
                continue
            if not queueable:
                continue
            roles[kind] = AgentRole(
                name=name,
                kind=kind,
                description=str(fm.get("description") or ""),
                path=path,
                label=str(fm.get("label") or ""),
                icon=str(fm.get("icon") or "◆"),
                blurb=str(fm.get("blurb") or ""),
                applies=str(fm.get("applies") or "any"),
                drained_by=drained,
                writes=str(fm.get("writes") or "none"),
                source=source,
                queueable=True,
            )
    for kind, meta in _BUILTIN_ENGINE.items():
        roles.setdefault(kind, AgentRole(
            name=kind, kind=kind, description=meta["blurb"],
            path=plugin_root / "agents" / "autoform-worker.md",
            label=meta["label"], icon=meta["icon"], blurb=meta["blurb"],
            applies=meta["applies"], drained_by="engine", source="builtin",
        ))
    return roles


#: Palette metadata for the two deterministic kinds, used only when no role file
#: declares them. A file that sets ``kind: worker`` overrides this entirely.
_BUILTIN_ENGINE = {
    "reviewer": {"label": "Reviewer", "icon": "⚖", "applies": "any",
                 "blurb": "re-review this node (the three-axis jury)"},
    "worker": {"label": "Worker", "icon": "⛏", "applies": "any",
               "blurb": "formalize / fill a sorry here"},
}


def agent_kinds(plugin_root: Path, project: Path | None = None) -> tuple[str, ...]:
    """Kinds a host-CLI agent drains (everything the engine doesn't own)."""
    return tuple(sorted(k for k, r in discover(plugin_root, project).items()
                        if r.drained_by == "agent"))


@dataclass
class Registry:
    """Cached view of the roles for one (plugin, project) pair."""

    plugin_root: Path
    project: Path | None = None
    roles: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.roles:
            self.roles = discover(self.plugin_root, self.project)

    def get(self, kind: str) -> AgentRole | None:
        return self.roles.get(kind)

    def agent_kinds(self) -> tuple[str, ...]:
        return tuple(sorted(k for k, r in self.roles.items() if r.drained_by == "agent"))

    def palette(self) -> list[dict]:
        engine = [r for r in self.roles.values() if r.drained_by == "engine"]
        agents = [r for r in self.roles.values() if r.drained_by == "agent"]
        return ([r.to_palette_entry() for r in sorted(engine, key=lambda r: r.kind)]
                + [r.to_palette_entry() for r in sorted(agents, key=lambda r: r.kind)])
