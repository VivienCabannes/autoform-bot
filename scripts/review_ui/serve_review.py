#!/usr/bin/env python3
"""Local review server for the DAG-native review surface.

Serves the three review screens over a stdlib ThreadingHTTPServer bound to
``127.0.0.1`` (local only — never exposed). The graph and the *built* blueprint
fragments are read-only; the only files this server ever writes are the sidecar
``review_status.json`` (the single source of truth for verdicts) and the dispatch
queue ``task_queue.json`` (the orchestrate engine's inbox, via ``/api/request``).

Three screens:
  * ``GET /``               — home: the dep-graph recolored by effective verdict
                              (AI-only dashed, human solid, tainted hatched, mathlib
                              lanes), with the coverage bar + trust-frontier header.
  * ``GET /cluster/<id>``   — a tier-1 cluster's children + roll-up ("review deck").
  * ``GET /node/<id>``      — the packet: rendered blueprint theorem env (left) +
                              source_refs / mathlib decl / kernel evidence (right) +
                              the verdict panel.

API:
  * ``GET  /api/state``         — computed verdicts + taint + coverage + frontier.
  * ``GET  /api/dot``           — ``?tier=<1|2>&expand=cX,cY`` -> ``{dot, id_by_slug,
                                  kinds}`` so the client re-renders (tier toggle /
                                  in-place cluster expansion) with a transition.
  * ``GET  /api/agents``        — the current read-only ``agents_status.json`` live
                                  activity feed, recomputed per call.
  * ``POST /api/verdict/<id>``  — write the human slot, recompute taint, return the
                                  delta (new effective verdicts + tainted set).
  * ``GET  /assets/*``          — review.css / review.js (+ any static asset).

Inputs (read-only): ``graph.json``, ``informal_content/<id>.md``, the built
blueprint (``dep_graph_document.html`` for the ``div.thm#<slug>`` fragments), an
optional ``kernel/<id>.txt`` (``#print axioms`` dump), and the sidecar.

Run:
    python serve_review.py --graph path/to/graph.json
    python serve_review.py --graph graph.json --port 8765 --open
"""
from __future__ import annotations

import argparse
import html as htmllib
import json
import os
import re
import secrets
import sys
import tempfile
import urllib.parse
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional

_HERE = Path(__file__).resolve().parent
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))  # scripts/ on path for export_blueprint

import fslock  # noqa: E402 — cross-process lock shared with dispatch_runner/_queue
import review_model as rm  # noqa: E402

# Repo root = .../scripts/review_ui -> up two. Assets live at <root>/assets/review/.
_REPO_ROOT = _HERE.parent.parent
_ASSETS_DIR = _REPO_ROOT / "assets" / "review"

# Regex to pull a single built blueprint fragment <div class="thm" id="slug" ...>
# ... </div> out of dep_graph_document.html. The exported template renders each
# tier-2 node as `<div class="thm" id="{{ slug }}" ...>` (MathJax already run), so
# we inject that fragment verbatim — never regenerating the informalization.
_THM_OPEN = '<div class="thm"'

# Whole-word match for an incompleteness marker in Lean: `sorry`, `admit`, or the
# raw `sorryAx`. `\b` anchors keep it from firing inside an identifier (e.g.
# `sorryHandler`, `my_admit`); the line scanner below first strips `--` comments so a
# `-- sorry` note never counts. This is the live "not implemented yet" detector.
# The ``(?!-)`` tail rejects the hyphenated word form (``sorry-free``, ``admit-…``):
# a real ``sorry``/``admit`` token in code is never followed by ``-``.
_SORRY_RE = re.compile(r"\b(?:sorry|admit|sorryAx)\b(?!-)")
# Block comments /- … -/ (incl. /-! … -/ module docstrings) are stripped before the
# line scan so prose like "inspection-verified sorry-free" or "we admit …" inside a
# docstring never trips the detector. Non-greedy; nested blocks are best-effort.
_BLOCK_COMMENT_RE = re.compile(r"/-.*?-/", re.DOTALL)


def _lean_has_sorry(text: str) -> bool:
    """Best-effort: does this Lean source contain a real ``sorry``/``admit``/
    ``sorryAx``?

    Strips block comments (``/- … -/``) and each line's ``--`` trailing comment
    before matching, and the whole-word regex rejects identifiers (``sorryHandler``,
    ``my_admit``) and the hyphenated prose form (``sorry-free``). A false negative
    here only means a node is *not* flagged violet, never that a real gap is hidden
    where it matters for trust — the detector lights up incomplete modules, it is not
    a Lean parser.
    """
    text = _BLOCK_COMMENT_RE.sub(" ", text)
    for line in text.splitlines():
        code = line.split("--", 1)[0]
        if _SORRY_RE.search(code):
            return True
    return False


# ---------------------------------------------------------------------------
# agent dispatch — the palette + bounded, write-only task queue
# ---------------------------------------------------------------------------

# The fixed registry of dispatchable agents, served to the client so the activity
# panel can render a drag palette. Each entry is {id, label, icon, blurb, applies}.
# This is the ONLY set of agents a /api/request may enqueue — an `agent` outside
# these ids is a 400. The server merely WRITES a queue entry; it NEVER spawns the
# agent or runs claude — an external orchestrator consumes task_queue.json.
AGENT_PALETTE = [
    # Deterministic-engine kinds (dispatch_runner drains these):
    {"id": "reviewer", "label": "Reviewer", "icon": "⚖",
     "blurb": "re-review this node (the review jury)", "applies": "any"},
    {"id": "worker", "label": "Worker", "icon": "⛏",
     "blurb": "formalize / fill a sorry here", "applies": "any"},
    # Model-driven kinds (the /autoform:orchestrate agent runs these as Task subagents):
    {"id": "planner", "label": "Planner", "icon": "◷",
     "blurb": "split + check + review this cluster's sub-DAG", "applies": "tier1"},
    {"id": "graphreview", "label": "Graph reviewer", "icon": "🔗",
     "blurb": "audit & fix dependency edges here", "applies": "tier1"},
    {"id": "contentreview", "label": "Content reviewer", "icon": "📝",
     "blurb": "check prose faithfulness vs sources", "applies": "tier1"},
    {"id": "holistic", "label": "Holistic review", "icon": "🔭",
     "blurb": "big-picture pass over the whole graph", "applies": "any"},
    {"id": "mathcheck", "label": "Mathlib check", "icon": "🔎",
     "blurb": "is this concept already in Mathlib?", "applies": "any"},
    # Raised by the engine when a worker hits a wall; the orchestrate agent triages it
    # (a human may also drop it to ask the orchestrator to look at a node).
    {"id": "escalation", "label": "Escalation", "icon": "⚑",
     "blurb": "a worker hit a wall here — the orchestrator triages (grow the DAG / fix / surface)", "applies": "any"},
]

# Set of valid agent ids (membership test for /api/request validation).
_PALETTE_IDS = {a["id"] for a in AGENT_PALETTE}

# ---------------------------------------------------------------------------
# prover backend selection — shared with the /autoform:set-backend command via the
# SAME config file (~/.autoform/config.json, override with $AUTOFORM_CONFIG). The
# dashboard reads it for the backend dropdown and writes it when the user flips it,
# so the UI and the CLI stay in sync. Backend is also the billing path: max = the Max
# subscription (no API tokens), aristotle = Harmonic, codex = its own (planned).
# Mirrors plugins/autoform/scripts/backend_config.py's registry.
# ---------------------------------------------------------------------------
BACKENDS = {
    "max": {"label": "Claude Max", "available": True,
            "billing": "Max subscription · no API tokens"},
    "aristotle": {"label": "Aristotle", "available": True,
                  "billing": "Harmonic · ARISTOTLE_API_KEY"},
    "codex": {"label": "Codex", "available": False,
              "billing": "Codex · its own auth (planned)"},
}
_DEFAULT_BACKEND = "max"


def _backend_config_path() -> Path:
    return Path(os.environ.get(
        "AUTOFORM_CONFIG", str(Path.home() / ".autoform" / "config.json")))


def get_backend() -> str:
    """The persisted prover backend, or ``max`` if unset/unreadable/unknown."""
    try:
        data = json.loads(_backend_config_path().read_text())
        if data.get("backend") in BACKENDS:
            return data["backend"]
    except Exception:
        pass
    return _DEFAULT_BACKEND


def set_backend(backend: str) -> None:
    """Persist ``backend`` to the shared config (atomic). Raises ValueError if unknown."""
    if backend not in BACKENDS:
        raise ValueError(f"unknown backend {backend!r}")
    p = _backend_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(p.read_text())
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    data["backend"] = backend
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def _backend_payload() -> dict:
    """The {current, options} the dashboard's backend dropdown renders from."""
    return {
        "current": get_backend(),
        "options": [{"id": k, "label": v["label"], "available": v["available"],
                     "billing": v["billing"]} for k, v in BACKENDS.items()],
    }

# Hard cap on the dispatch queue (bounded write — the orchestrator drains it).
TASK_QUEUE_CAP = 200

# Statuses an entry may carry; only a *queued* task may be cancelled, and a
# queued/running task with the same agent+node blocks a duplicate enqueue.
_ACTIVE_STATUSES = {"queued", "running"}

# Sentinel returned by the body reader on un-parseable JSON (distinct from a valid
# ``{}`` body) so a POST handler answers 400 rather than treating it as empty.
_BAD_JSON = object()

# ---------------------------------------------------------------------------
# CSRF protection. The server binds 127.0.0.1, but any webpage the user visits
# can still fire cross-origin "simple" POSTs at localhost (no CORS preflight),
# forging HUMAN VERDICTS or enqueueing Max-billed tasks. Two independent checks
# gate every POST (GETs stay unauthenticated, they expose nothing sensitive):
#
#   * a per-process random token, generated at startup, embedded in every served
#     page as ``window.__RV_TOKEN__`` and required back in the ``X-Review-Token``
#     header — a foreign origin can *send* a request but can never *read* the
#     page, so it can never learn the token;
#   * a Host-header allowlist (127.0.0.1 / localhost / [::1], with the bound
#     port) — blocks DNS-rebinding, where an attacker points their own hostname
#     at 127.0.0.1 to make the dashboard "same-origin" with their page.
# ---------------------------------------------------------------------------
_API_TOKEN = secrets.token_hex(16)

# Hostnames a request may address this server by (loopback spellings only).
_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _host_denied(host: Optional[str], bound_port: int) -> Optional[str]:
    """Why the Host header is unacceptable, or None if it is fine.

    Accepts exactly the loopback names, with either no port or the bound port
    (``[::1]:port`` bracket form included). Anything else — a public hostname, a
    rebinding domain, a wrong port — is rejected.
    """
    host = (host or "").strip()
    if not host:
        return "missing Host header"
    if host.startswith("["):                      # [::1] / [::1]:port
        name, _, rest = host.partition("]")
        name, port = name[1:], rest.lstrip(":")
    else:
        name, _, port = host.partition(":")
    if name.lower() not in _ALLOWED_HOSTS:
        return f"Host {host!r} is not loopback"
    if port and port != str(bound_port):
        return f"Host {host!r} does not match the bound port {bound_port}"
    return None


def _bounded_queue(queue: List[dict]) -> List[dict]:
    """Apply ``TASK_QUEUE_CAP``: drop the OLDEST finished (non-queued/running)
    entries until the total fits — never a live entry. Order is preserved. If the
    live entries alone exceed the cap, everything live is still kept (the cap
    bounds *history*, not real pending work)."""
    overflow = len(queue) - TASK_QUEUE_CAP
    if overflow <= 0:
        return queue
    kept: List[dict] = []
    for t in queue:
        if (overflow > 0 and isinstance(t, dict)
                and t.get("status") not in _ACTIVE_STATUSES):
            overflow -= 1
            continue
        kept.append(t)
    return kept


# ---------------------------------------------------------------------------
# project context — resolves all the read-only inputs around graph.json
# ---------------------------------------------------------------------------

class Project:
    """Resolves the read-only inputs + owns the single sidecar write.

    Everything is recomputed from disk on each request (cheap, and keeps the server
    correct if the graph or a re-run of the jury changes the files under it).
    """

    def __init__(self, graph_path: Path):
        self.graph_path = graph_path.resolve()
        self.root = self.graph_path.parent
        self.content_dir = self.root / "informal_content"
        self.kernel_dir = self.root / "kernel"
        self.sidecar_path = self.root / "review_status.json"
        # Live activity feed, read-only, sitting next to graph.json. The orchestrator
        # may write it while a run is in flight; this server only ever reads it.
        self.agents_path = self.root / "agents_status.json"
        # Dispatch queue, sitting next to graph.json. This is the SECOND deliberate
        # write this server performs (beyond review_status.json): an append-only-ish
        # list of task requests an external orchestrator consumes. The server writes
        # it but NEVER spawns an agent / runs claude — it only dispatches.
        self.task_queue_path = self.root / "task_queue.json"
        # Built blueprint dep-graph page (source of div.thm#<slug> fragments).
        self.blueprint_html = (
            self.root / "blueprint_export" / "blueprint" / "web"
            / "dep_graph_document.html"
        )
        self._slug_cache: Optional[Dict[str, str]] = None
        # Root of the Lean sources a module node maps into (module A.B.C ->
        # <lean_root>/A/B/C.lean). Set from --lean-root or graph metadata.lean_root;
        # None disables the Lean-source panel. Read-only, path-traversal-guarded.
        self.lean_root: Optional[Path] = None

    # --- graph + sidecar ---
    def nodes(self):
        nodes, _meta = rm.load_graph(self.graph_path)
        return nodes

    def metadata(self):
        _nodes, meta = rm.load_graph(self.graph_path)
        return meta

    def sidecar(self) -> dict:
        return rm.load_sidecar(self.sidecar_path)

    def write_sidecar(self, data: dict) -> None:
        """The one and only write this server performs — atomic, via review_model."""
        rm.save_sidecar(self.sidecar_path, data)

    def agents(self) -> dict:
        """The current live activity feed, recomputed from disk each call (read-only;
        never raises — an absent feed degrades to an idle orchestrator)."""
        return rm.load_agents(self.agents_path)

    # --- the dispatch queue (the second deliberate write) ---
    def task_queue(self) -> List[dict]:
        """The current dispatch queue, read from ``task_queue.json`` next to graph.json.

        A list of task-request records the orchestrator consumes. Absent file,
        unreadable file, corrupt JSON, or a non-list root all degrade to ``[]`` — the
        dashboard must never error just because nothing has been dispatched yet. Never
        raises. Non-dict entries are dropped so a malformed queue can't crash a render.
        """
        p = self.task_queue_path
        if not p.is_file():
            return []
        try:
            data = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(data, list):
            return []
        return [t for t in data if isinstance(t, dict)]

    def write_task_queue(self, queue: List[dict]) -> None:
        """Atomically persist the dispatch queue (temp file + ``os.replace``), capped at
        ``TASK_QUEUE_CAP`` entries — the orchestrator's inbox, bounded and crash-safe.

        This is the SECOND deliberate write this server performs (beyond the sidecar).
        It writes ONLY ``task_queue.json``; it never mutates the graph, the
        informal_content, the agents feed, or the review sidecar, and it never spawns
        an agent.

        The cap only ever trims **finished** history (``done``/``failed``, oldest
        first) — a live ``queued``/``running`` entry is NEVER dropped: silently
        losing one would cancel real pending work and shrink the per-node escalation
        history the engine's circuit-breaker counts. So the file may exceed the cap
        when more than ``TASK_QUEUE_CAP`` entries are live (the engine drains them).
        """
        bounded = _bounded_queue(list(queue))
        payload = json.dumps(bounded, ensure_ascii=False, indent=2)
        # Unique temp name (mkstemp) — a fixed <name>.tmp is a write race with the
        # engine, which swaps the same file. Callers doing load-mutate-save hold
        # fslock.locked(task_queue_path) around the whole cycle.
        fd, tmp = tempfile.mkstemp(dir=str(self.task_queue_path.parent),
                                   prefix=self.task_queue_path.name + ".",
                                   suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
            os.replace(tmp, self.task_queue_path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def slug_map(self) -> Dict[str, str]:
        # Slugs are a deterministic function of the id set; recompute per request so
        # an added node is reflected without a restart.
        import export_blueprint as eb
        return eb.build_slug_map(self.nodes())

    # --- the three read-only artifacts of a node ---
    def blueprint_fragment(self, slug: str) -> Optional[str]:
        """Extract the built ``<div class="thm" id="<slug>">...</div>`` fragment.

        Returns None if the blueprint has not been built or the node has no env.
        MathJax has already run in the built page, so the fragment is injected
        verbatim — we never regenerate the informalization.
        """
        if not self.blueprint_html.is_file():
            return None
        text = self.blueprint_html.read_text(errors="replace")
        needle = f'id="{slug}"'
        # Find a `<div class="thm" ... id="slug"` open tag, then balance divs.
        idx = 0
        while True:
            open_at = text.find(_THM_OPEN, idx)
            if open_at == -1:
                return None
            tag_end = text.find(">", open_at)
            if tag_end == -1:
                return None
            open_tag = text[open_at:tag_end]
            if needle in open_tag:
                return _extract_balanced_div(text, open_at)
            idx = tag_end + 1

    def informal_md(self, node_id: str, node: dict) -> Optional[str]:
        """Raw informal_content markdown for a node (fallback when no blueprint)."""
        cand = []
        cpath = node.get("content")
        if cpath:
            cand.append(self.root / cpath)
        cand.append(self.content_dir / f"{node_id}.md")
        for c in cand:
            if c.is_file():
                return c.read_text(errors="replace")
        return None

    def lean_source(self, node_id: str, node: dict,
                    max_lines: int = 600) -> Optional[dict]:
        """The real Lean source for a *module* node, read from ``lean_root``.

        A module ``A.B.C`` maps to ``<lean_root>/A/B/C.lean`` (Lean's file layout);
        a node may instead carry an explicit relative ``lean_file``. Returns
        ``{text, rel, truncated, total}`` (display capped at ``max_lines``) or None
        when there is no source. Read-only and path-traversal-guarded: the resolved
        file must live under ``lean_root``, and only ``kind == "module"`` nodes (or
        nodes with an explicit ``lean_file``) are eligible — so a crafted node id can
        never read outside the Lean tree.
        """
        rel = node.get("lean_file")
        if not rel:
            if (node.get("kind") or "").lower() != "module":
                return None
            rel = node_id.replace(".", "/") + ".lean"
        root = self.lean_root
        if root is None:
            mr = (self.metadata() or {}).get("lean_root")
            root = Path(mr) if mr else None
        if root is None:
            return None
        root = root.resolve()
        target = (root / rel).resolve()
        try:
            target.relative_to(root)            # reject any ../ escape
        except ValueError:
            return None
        if not target.is_file():
            return None
        text = target.read_text(errors="replace")
        lines = text.splitlines()
        truncated = len(lines) > max_lines
        return {
            "text": "\n".join(lines[:max_lines]),
            "rel": rel,
            "truncated": truncated,
            "total": len(lines),
        }

    def _lean_root(self) -> Optional[Path]:
        """Resolve the root of the Lean sources, or None when the feature is inert.

        Same resolution the Lean-source panel uses: the explicit ``--lean-root``
        (``self.lean_root``) wins, else graph ``metadata.lean_root``. ``None`` /
        missing / unreadable metadata → ``None`` (the sorry feature simply does
        nothing). Never raises.
        """
        root = self.lean_root
        if root is None:
            try:
                mr = (self.metadata() or {}).get("lean_root")
            except Exception:
                mr = None
            root = Path(mr) if mr else None
        if root is None:
            return None
        try:
            root = root.resolve()
        except OSError:
            return None
        return root if root.is_dir() else None

    def sorry_set(self, nodes: Dict[str, dict]) -> set:
        """The set of node ids whose Lean is **incomplete** (a live ``sorry`` scan).

        When ``lean_root`` is configured (``--lean-root`` or graph
        ``metadata.lean_root``), scan every ``*.lean`` file under it for a whole-word
        ``sorry`` / ``admit`` / ``sorryAx`` outside a ``--`` line comment, then map each
        flagged file to its **module id** and propagate to that module's ancestors:

          1. **file → module id.** A file ``<lean_root>/A/B/C.lean`` is module
             ``A.B.C`` (Lean's package layout). A node may instead pin an explicit
             relative ``lean_file``; such a node's id is used directly for that file
             (an explicit ``lean_file`` overrides the path-derived id), so a module
             whose graph id is not its dotted path is still matched.
          2. **keep only real nodes.** A derived/declared module id is added to the
             result only if it is actually a node in the graph — a ``.lean`` file with
             no corresponding node contributes nothing (no phantom ids).
          3. **ancestor propagation.** For every flagged module, walk ``node['parent']``
             up the graph topology and add each ancestor (its unit, then its cluster,
             …). An incomplete module therefore turns its parent unit and cluster violet
             too, matching ``color_state``'s "a parent is sorry iff any descendant is".

        Recomputed per request (cheap), and **degrades gracefully**: ``lean_root``
        ``None`` / missing → ``set()``; an unreadable file is skipped; a malformed
        node ``parent`` chain (or a cycle) is bounded so the walk always terminates.
        ``lean_root`` absent ⇒ the feature is inert and the whole surface reproduces
        its pre-sorry behavior exactly.
        """
        root = self._lean_root()
        if root is None:
            return set()

        # Explicit pins: lean_file (relative, as resolved against root) -> node id.
        # An explicit lean_file wins over the path-derived module id for that file.
        explicit: Dict[Path, str] = {}
        for nid, node in nodes.items():
            rel = (node or {}).get("lean_file")
            if not rel:
                continue
            try:
                tgt = (root / rel).resolve()
                tgt.relative_to(root)            # ignore any ../ escape
            except (OSError, ValueError):
                continue
            explicit[tgt] = nid

        flagged: set = set()
        try:
            lean_files = sorted(root.rglob("*.lean"))
        except OSError:
            lean_files = []
        for f in lean_files:
            try:
                text = f.read_text(errors="replace")
            except OSError:
                continue
            if not _lean_has_sorry(text):
                continue
            fr = f.resolve()
            # Map this file to a module id: an explicit lean_file pin wins, else the
            # dotted path relative to lean_root (A/B/C.lean -> A.B.C).
            nid = explicit.get(fr)
            if nid is None:
                try:
                    relpath = fr.relative_to(root)
                except ValueError:
                    continue
                nid = ".".join(relpath.with_suffix("").parts)
            # Only a real graph node counts (no phantom ids from stray .lean files).
            if nid in nodes:
                flagged.add(nid)

        # Propagate each flagged module to its ancestors (parent unit, cluster, …) by
        # walking node['parent'] up the topology, bounded against a malformed cycle.
        result: set = set()
        for nid in flagged:
            cur: Optional[str] = nid
            seen: set = set()
            while cur and cur in nodes and cur not in seen:
                seen.add(cur)
                result.add(cur)
                parent = nodes[cur].get("parent")
                cur = parent if isinstance(parent, str) else None
        return result

    def kernel_evidence(self, node_id: str) -> Optional[str]:
        """The ``#print axioms`` dump for a node, if a ``kernel/<id>.txt`` exists."""
        p = self.kernel_dir / f"{node_id}.txt"
        if p.is_file():
            return p.read_text(errors="replace")
        return None


def _extract_balanced_div(text: str, open_at: int) -> str:
    """Return the substring covering one balanced <div>...</div> from ``open_at``."""
    depth = 0
    i = open_at
    n = len(text)
    div_open = re.compile(r"<div\b", re.IGNORECASE)
    div_close = re.compile(r"</div\s*>", re.IGNORECASE)
    while i < n:
        mo = div_open.match(text, i)
        mc = div_close.match(text, i)
        if mo:
            depth += 1
            i = text.find(">", i)
            if i == -1:
                break
            i += 1
        elif mc:
            depth -= 1
            i = mc.end()
            if depth == 0:
                return text[open_at:i]
        else:
            i += 1
    return text[open_at:]


# ---------------------------------------------------------------------------
# HTML rendering of the three screens (server-side; assets add interactivity)
# ---------------------------------------------------------------------------

_E = htmllib.escape


def _page(title: str, body: str, bootstrap: str = "") -> bytes:
    """Wrap a screen body in the shared shell (links review.css/js).

    Every page carries the per-process CSRF token (``window.__RV_TOKEN__``) so
    the client JS can send it back as ``X-Review-Token`` on its POSTs."""
    boot = (f"<script>window.__RV_TOKEN__ = {json.dumps(_API_TOKEN)};"
            f"{bootstrap}</script>")
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{_E(title)}</title>"
        "<link rel='stylesheet' href='/assets/review.css'>"
        "<script>window.MathJax={tex:{inlineMath:[['$','$'],['\\\\(','\\\\)']],"
        "displayMath:[['$$','$$'],['\\\\[','\\\\]']]},"
        "options:{skipHtmlTags:['script','noscript','style','textarea','pre','code']}};</script>"
        "<script async src='https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js'></script>"
        "</head><body>"
        "<header class='rv-header'><a class='rv-home' href='/'>review</a>"
        f"<span class='rv-title'>{_E(title)}</span></header>"
        f"<main class='rv-main'>{body}</main>"
        f"{boot}"
        "<script src='/assets/dep_graph_core.js'></script>"
        "<script src='/assets/review.js'></script>"
        "</body></html>"
    ).encode("utf-8")


def _render_md(md: str) -> str:
    """Minimal, safe Markdown -> HTML for the informal_content fallback: headings and
    paragraphs only, HTML-escaped, with ``$...$`` / ``$$...$$`` kept intact for MathJax
    (so it must NOT be wrapped in <pre>, which MathJax is configured to skip)."""
    out = []
    para = []

    def _flush():
        if para:
            out.append("<p>" + "<br>".join(_E(x) for x in para) + "</p>")
            para.clear()

    for line in md.strip().split("\n"):
        s = line.strip()
        m = re.match(r"^(#{1,6})\s+(.*)$", s)
        if not s:
            _flush()
        elif m:
            _flush()
            lvl = min(len(m.group(1)) + 2, 6)
            out.append(f"<h{lvl}>{_E(m.group(2).strip())}</h{lvl}>")
        else:
            para.append(s)
    _flush()
    return "\n".join(out) or "<p><em>(empty)</em></p>"


# Human-facing labels for the tier toggle: tier number -> label.
TIER_LABELS = {1: "clusters", 2: "statements", 3: "declarations"}

# A flat tier with more than LARGE nodes is too big to render whole (the browser
# hangs). Instead of the full graph we serve a friendly placeholder + an entry-point
# picker (the client builds the picker from __RV_NODES__). Local views (focus/anchor)
# render a bounded neighborhood and so are never subject to this guard.
LARGE = 120

# How far a neighborhood reaches, and how many nodes it may contain. The local view is
# always bounded by NB_CAP so it loads fast; the anchor radius is clamped to NB_MAX_RADIUS.
NB_CAP = 60
NB_MIN_RADIUS = 1
NB_MAX_RADIUS = 3


def _clamp_radius(raw) -> int:
    """Resolve a ``?radius=`` query value, clamped to ``[NB_MIN_RADIUS, NB_MAX_RADIUS]``.

    Defaults to ``NB_MIN_RADIUS`` (1) when absent or non-integer; an out-of-range
    value is clamped, never rejected, so a bad/oversized radius still renders a
    bounded view rather than erroring.
    """
    try:
        k = int(raw)
    except (TypeError, ValueError):
        return NB_MIN_RADIUS
    return max(NB_MIN_RADIUS, min(NB_MAX_RADIUS, k))


def _parse_tier(raw, nodes: Dict[str, dict]) -> int:
    """Resolve a ``?tier=`` query value against the tiers actually present.

    Returns the requested tier when it occurs in the graph, else the lowest tier
    present (the default home view). A graph with no nodes degrades to tier 1.
    """
    present = rm.tiers_present(nodes)
    default = present[0] if present else 1
    if raw is None:
        return default
    try:
        want = int(raw)
    except (TypeError, ValueError):
        return default
    return want if want in present else default


def _topology(nodes: Dict[str, dict]) -> dict:
    """Tier-agnostic parent/children topology the client needs to drive the unroll.

    The DOT alone can't tell the client whether a *collapsed* node is a parent (so a
    click should unroll it) or a leaf (so a click should open its packet), nor which
    parent a tier-(N+1) child belongs to once a box is open (so a hidden target can
    pulse its collapsed parent). ``state.clusters`` answers this only for tier 1.

    So expose, for the whole graph (every tier):

      * ``children`` — ``{parent id: [direct child ids one tier down]}`` for every
        node that has at least one child (an entry's presence === "has children");
      * ``parents``  — ``{child id: parent id}`` (the inverse), so the client can map
        any node to the box it lives in.

    Both are derived from ``rm.child_ids`` (same rule the model expands by), so the
    client's "has children?" test is exactly the server's, at any tier.
    """
    children: Dict[str, list] = {}
    parents: Dict[str, str] = {}
    for nid in nodes:
        kids = rm.child_ids(nid, nodes)
        if kids:
            children[nid] = kids
            for k in kids:
                parents[k] = nid
    return {"children": children, "parents": parents}


def _tier_count(tier: int, nodes: Dict[str, dict]) -> int:
    """How many nodes sit at ``tier`` (drives the flat too-large guard)."""
    import export_blueprint as eb
    return sum(1 for n in nodes.values() if eb.node_tier(n) == tier)


def _placeholder_dot() -> str:
    """An empty placeholder DOT for the too-large flat guard.

    A valid-but-empty digraph: the client never renders it (it shows the picker card
    when ``__RV_TOO_LARGE__`` is set), and ``/api/dot`` returns it so a stray re-render
    request for a too-large flat tier can never trigger the 226-node graph. Carries a
    single invisible placeholder node so any d3/graphviz consumer parses cleanly.
    """
    return ('strict digraph "" {\n'
            '  graph [splines=line];\n'
            '  "__rv_placeholder__" [style=invis, label=""];\n'
            '}')


def _focus_payload(parent_id: Optional[str], nodes: Dict[str, dict]) -> Optional[dict]:
    """Build ``__RV_FOCUS__`` for ``?focus=<parentid>``: the parent, its display
    label, and the child ids one tier down (the members to highlight in context).

    Returns None when there is no focus or the parent is unknown, so the client
    simply renders the flat graph with no focus ring.
    """
    if not parent_id or parent_id not in nodes:
        return None
    parent = nodes[parent_id]
    return {
        "parent": parent_id,
        "label": parent.get("name") or parent_id,
        "members": rm.child_ids(parent_id, nodes),
    }


def _anchor_payload(anchor_id: Optional[str], radius: int,
                    nodes: Dict[str, dict]) -> Optional[dict]:
    """Build ``__RV_ANCHOR__`` for ``?anchor=<nodeid>&radius=K``: the centered node id
    and the (already clamped) radius. Returns None when there is no anchor or the
    anchor is unknown, so the client renders the flat graph with no anchor banner.
    """
    if not anchor_id or anchor_id not in nodes:
        return None
    return {"id": anchor_id, "radius": radius}


def _tier_nodes_payload(tier: int, nodes: Dict[str, dict]) -> list:
    """The tier-`tier` nodes as ``[{id,label,parent}]`` (sorted by id) for the
    too-large entry-point picker. Faithful to the data — real ids/labels/parents —
    so the client can filter and jump to ``/?tier=N&anchor=<id>`` without rendering
    the (too-large) graph.
    """
    import export_blueprint as eb
    out = []
    for nid in sorted(nodes):
        node = nodes[nid]
        if eb.node_tier(node) != tier:
            continue
        out.append({
            "id": nid,
            "label": node.get("name") or nid,
            "parent": node.get("parent"),
        })
    return out


def render_home(proj: Project, tier: Optional[int] = None,
                focus: Optional[str] = None,
                anchor: Optional[str] = None,
                radius: Optional[int] = None) -> bytes:
    nodes = proj.nodes()
    sidecar = proj.sidecar()
    # Live "not implemented" set: node ids whose Lean has a sorry/admit/sorryAx (plus
    # each one's ancestor unit/cluster). Empty when no lean_root is configured, so the
    # whole surface is unchanged in that case. Recomputed per request.
    sorry_set = proj.sorry_set(nodes)
    state = rm.compute_state(nodes, sidecar, sorry_set)
    # Resolve the requested tier against the tiers actually present; the default
    # home is the lowest tier present.
    present = rm.tiers_present(nodes)
    tier = _parse_tier(tier, nodes)

    # --- decide which graph to render ---
    # Three mutually-distinct cases at this tier:
    #   focus=<parent>  → a BOUNDED neighborhood of the parent's children (local view)
    #   anchor=<node>   → a BOUNDED neighborhood centered on one node (local view)
    #   flat (neither)  → the whole tier, UNLESS it is too large (> LARGE) — then a
    #                     placeholder + picker, never the full graph.
    focus_payload = _focus_payload(focus, nodes)
    radius = _clamp_radius(radius)
    anchor_payload = _anchor_payload(anchor, radius, nodes)

    only = None                  # the bounded subgraph (set of ids) or None for flat
    neighborhood_view = False    # True for a focus/anchor local view
    too_large = None             # __RV_TOO_LARGE__ payload for the flat guard
    tier_nodes = None            # __RV_NODES__ picker list for the flat guard

    if focus_payload is not None:
        # Modules of <unit> + immediate neighbors: seed on the unit's children, ±1 hop.
        members = focus_payload["members"]
        only = rm.neighborhood(set(members), nodes, tier, radius=1, cap=NB_CAP)
        neighborhood_view = True
    elif anchor_payload is not None:
        # Neighborhood of <anchor> within the clamped radius.
        only = rm.neighborhood({anchor_payload["id"]}, nodes, tier, radius, cap=NB_CAP)
        neighborhood_view = True
    else:
        # Flat tier view: guard a too-large tier — do NOT render the full graph.
        tier_count = _tier_count(tier, nodes)
        if tier_count > LARGE:
            too_large = {"tier": tier, "count": tier_count, "threshold": LARGE}
            tier_nodes = _tier_nodes_payload(tier, nodes)

    # The flat tier-N graph starts fully collapsed (no nodes expanded); the client
    # unrolls in place by re-fetching /api/dot with an `expand` set. A local view
    # (focus/anchor) renders the bounded `only` subgraph instead of the whole tier;
    # a too-large flat tier renders an empty placeholder DOT (the client shows the
    # picker card, never the 226-node graph).
    if too_large is not None:
        dot = _placeholder_dot()
    else:
        dot = rm.recolor_dot(nodes, sidecar, tier=tier, only=only,
                             sorry_set=sorry_set)
    slug_map = proj.slug_map()
    # id<->slug both directions so the client can route node clicks to /node/<id>.
    id_by_slug = {slug: nid for nid, slug in slug_map.items()}
    # kinds drive the client's shape/icon choices on re-render (box vs ellipse).
    kinds = {nid: (node.get("kind") or "theorem") for nid, node in nodes.items()}

    boot = (
        f"window.__RV_DOT__ = {json.dumps(dot)};"
        f"window.__RV_STATE__ = {json.dumps(state)};"
        f"window.__RV_IDBYSLUG__ = {json.dumps(id_by_slug)};"
        f"window.__RV_KINDS__ = {json.dumps(kinds)};"
        f"window.__RV_PALETTE__ = {json.dumps(rm.PALETTE)};"
        f"window.__RV_TIER__ = {tier};"
        # The tiers actually present (drives the tier toggle + valid jump targets).
        f"window.__RV_TIERS__ = {json.dumps(present)};"
        # Focus mode: {parent, label, members:[child ids one tier down]} or null.
        # In a too-large flat tier `focus` is still null; focus implies a local view.
        # The client adds a steady focus ring to the members + a "back" banner.
        f"window.__RV_FOCUS__ = {json.dumps(focus_payload)};"
        # Local-view flags. __RV_NEIGHBORHOOD__ is true for a focus/anchor view (the
        # `only` subgraph is bounded, so it renders normally). __RV_ANCHOR__ is
        # {id, radius} for an anchor view (drives the "±K hops · ‹ back" banner and
        # the "expand ±1 hop" control), else null.
        f"window.__RV_NEIGHBORHOOD__ = {json.dumps(neighborhood_view)};"
        f"window.__RV_ANCHOR__ = {json.dumps(anchor_payload)};"
        # Too-large flat guard: when set, the client renders a placeholder card +
        # entry-point picker (from __RV_NODES__) INSTEAD of the d3 graph — the DOT
        # is an empty placeholder, never the full N-node graph.
        #   __RV_TOO_LARGE__ = {tier, count, threshold} | null
        #   __RV_NODES__     = [{id,label,parent}] (the tier's nodes, for the picker)
        f"window.__RV_TOO_LARGE__ = {json.dumps(too_large)};"
        f"window.__RV_NODES__ = {json.dumps(tier_nodes)};"
        # The client owns the `expanded` set; it starts empty (home = collapsed).
        f"window.__RV_EXPANDED__ = [];"
        # Tier-agnostic parent/children topology so the client can tell a parent
        # (click → unroll) from a leaf (click → packet) at ANY tier, and map a
        # tier-(N+1) child to its (collapsed) parent box for the pulse.
        f"window.__RV_TOPO__ = {json.dumps(_topology(nodes))};"
        # Tells the client an /api/agents feed exists and where to poll it. The
        # Activity-panel HTML/JS is built client-side (Frontend phase), not here —
        # we only expose the data hooks + the container div below.
        f"window.__RV_AGENTS_URL__ = {json.dumps('/api/agents')};"
        f"window.__RV_DOT_URL__ = {json.dumps('/api/dot')};"
    )
    cov = state["coverage"]
    frontier = state["trust_frontier"]
    # --- compact mission-control header: project title · coverage · frontier · dial ---
    headerbar = (
        "<section class='rv-headerbar'>"
        "<div class='rv-headerbar-main'>"
        "<div class='rv-coverage'>"
        f"<span class='rv-cov-label'>coverage</span>"
        f"<div class='rv-cov-bar'><div class='rv-cov-fill' "
        f"style='width:{cov['fraction']*100:.0f}%'></div></div>"
        f"<span class='rv-cov-num'>{cov['reviewed']}/{cov['total']} reviewed"
        f" · {cov['human_confirmed']} human-confirmed</span>"
        "</div>"
        f"<div class='rv-frontier'><span class='rv-fr-label'>trust frontier</span> "
        + (", ".join(f"<a href='/node/{urllib.parse.quote(f)}'>{_E(f)}</a>"
                     for f in frontier) if frontier
           else "<em>none yet — no sink rests on a fully-clean closure</em>")
        + "</div>"
        "</div>"
        f"<div class='rv-dial'>dial: <strong>{_E(state['dial'])}</strong></div>"
        "</section>"
    )
    tiertoggle = _tiertoggle_html(present, tier)
    # Dashboard layout: header strip on top, then a two-column flex shell — the
    # Activity panel (sidebar) beside the tier-1 DAG centerpiece, with the legend
    # + tier toggle living in the graph column. The Activity panel HTML is built
    # client-side from /api/agents (review.js); the server provides the empty
    # mount + data hooks (in `boot`).
    body = (
        headerbar
        + "<div class='rv-dash'>"
        + "<aside id='rv-activity' class='rv-activity' "
          "data-agents-url='/api/agents'>"
          "<div class='rv-act-loading'>connecting to activity feed…</div>"
          "</aside>"
        + "<div class='rv-graphcol'>"
        + tiertoggle
        + _legend_html()
        # Focus banner ("Showing … in context · ‹ back"): hidden by default, the
        # client fills + reveals it when __RV_FOCUS__ is present. Reliable HTML, so
        # the context cue never depends on SVG injection.
        + "<div id='rv-focus-banner' class='rv-focus-banner' "
          "style='display:none'></div>"
        # Expanded-nodes bar: one chip per expanded node (collapse + open-in-tier-N+1).
        # The client builds it from its `expanded` set; empty/hidden until a node is
        # unrolled. Plain HTML above the graph — never injected into the SVG.
        + "<div id='rv-expanded-bar' class='rv-expanded-bar' "
          "style='display:none'></div>"
        + "<div id='rv-graph' class='rv-graph'>"
          "<div class='rv-graph-loading'>rendering dependency graph…</div>"
          "</div>"
        + "</div>"
        + "</div>"
    )
    return _page("dependency graph", body, boot)


def _legend_html() -> str:
    return (
        "<div class='rv-legend'>"
        "<span class='rv-key rv-in_mathlib'>in Mathlib</span>"
        "<span class='rv-key rv-clean'>ours · clean</span>"
        "<span class='rv-key rv-flagged'>flagged</span>"
        "<span class='rv-key rv-rejected'>rejected</span>"
        "<span class='rv-key rv-sorry'>sorry / not implemented</span>"
        "<span class='rv-key rv-grey'>unreviewed</span>"
        "<span class='rv-key rv-dash'>AI-only (dashed)</span>"
        "<span class='rv-key rv-solid'>human-confirmed</span>"
        "<span class='rv-key rv-hatch'>tainted</span>"
        "<span class='rv-lanes'>lanes: in-mathlib (bottom) → missing (top)</span>"
        "</div>"
    )


def _tiertoggle_html(present: list, tier: int) -> str:
    """The tier toggle, listing **exactly** the tiers present in the graph, each
    labelled ``N · <label>`` (1 · clusters / 2 · statements / 3 · declarations).

    The current tier is a static span; the others are links to ``/?tier=N``. A hint
    to unroll is shown on any tier that has a deeper tier to drill into.
    """
    parts = ["<div class='rv-tiertoggle'><span class='rv-tt-label'>view</span>"]
    for t in present:
        label = TIER_LABELS.get(t, f"tier {t}")
        text = f"{t} · {label}"
        if t == tier:
            parts.append(f"<span class='rv-tt rv-tt-on'>{_E(text)}</span>")
        else:
            parts.append(f"<a class='rv-tt' href='/?tier={t}'>{_E(text)}</a>")
    # A hint to unroll appears whenever there is a deeper tier to drill into.
    if present and tier != present[-1]:
        parts.append("<span class='rv-tt-hint'>click a node to unroll it · "
                     "shift-click for details</span>")
    else:
        parts.append("<span class='rv-tt-hint'>click a node for its details</span>")
    parts.append("</div>")
    return "".join(parts)


def render_cluster(proj: Project, cluster_id: str) -> bytes:
    nodes = proj.nodes()
    if cluster_id not in nodes:
        return _page("unknown cluster",
                     f"<p class='rv-err'>No cluster {_E(cluster_id)}.</p>")
    sidecar = proj.sidecar()
    roll = rm.cluster_rollup(cluster_id, nodes, sidecar)
    rows = []
    for cid in roll["children"]:
        v = roll["child_verdicts"][cid]
        src = rm.review_source(cid, sidecar) or "—"
        rows.append(
            f"<tr class='rv-row rv-{v}'>"
            f"<td><a href='/node/{urllib.parse.quote(cid)}'>{_E(cid)}</a></td>"
            f"<td class='rv-verdict-cell'>{_E(v)}</td>"
            f"<td>{_E(src)}</td></tr>"
        )
    counts = roll["counts"]
    body = (
        f"<h2 class='rv-cluster-rollup rv-{roll['verdict']}'>"
        f"cluster roll-up: {_E(roll['verdict'])}</h2>"
        f"<p class='rv-rollup-rule'>A cluster is clean only if every child is clean;"
        f" any flagged/rejected child ⇒ flagged.</p>"
        f"<p class='rv-counts'>clean {counts['clean']} · flagged {counts['flagged']}"
        f" · rejected {counts['rejected']} · unreviewed {counts['unreviewed']}</p>"
        "<table class='rv-table'><thead><tr><th>statement</th><th>verdict</th>"
        "<th>source</th></tr></thead><tbody>"
        + ("".join(rows) if rows
           else "<tr><td colspan='3'><em>no tier-2 children yet</em></td></tr>")
        + "</tbody></table>"
    )
    return _page(f"cluster · {cluster_id}", body)


def render_node(proj: Project, node_id: str) -> bytes:
    nodes = proj.nodes()
    if node_id not in nodes:
        return _page("unknown node",
                     f"<p class='rv-err'>No node {_E(node_id)}.</p>")
    node = nodes[node_id]
    sidecar = proj.sidecar()
    slug = proj.slug_map().get(node_id, node_id)
    scorecard = rm.node_scorecard(node_id, sidecar)

    # left column: built blueprint env (preferred) else raw informal markdown.
    fragment = proj.blueprint_fragment(slug)
    if fragment:
        left = f"<div class='rv-thm-env'>{fragment}</div>"
    else:
        md = proj.informal_md(node_id, node)
        if md:
            left = ("<div class='rv-thm-env rv-thm-raw'><p class='rv-note'>"
                    "blueprint not built — rendering informal_content.</p>"
                    f"{_render_md(md)}</div>")
        else:
            left = ("<div class='rv-thm-env'><em>No blueprint fragment or "
                    "informal content for this node.</em></div>")

    # right column: source_refs (verbatim) + mathlib decls + kernel evidence.
    right = _node_meta_html(proj, node_id, node, scorecard)

    # The node's own tier (and the tiers present) so the packet can offer a
    # "view neighborhood ▸" link back to the local view at the RIGHT tier:
    #   /?tier=<this node's tier>&anchor=<id>
    # — re-centering the bounded neighborhood on this node. node_tier is the same
    # rule the model/exporter use everywhere (node["tier"], default 2).
    import export_blueprint as eb
    node_tier = eb.node_tier(node)
    present = rm.tiers_present(nodes)
    boot = (
        f"window.__RV_NODE__ = {json.dumps(node_id)};"
        f"window.__RV_SCORECARD__ = {json.dumps(scorecard)};"
        # Drives the packet's "view neighborhood ▸" link (→ /?tier=<tier>&anchor=<id>).
        f"window.__RV_NODE_TIER__ = {json.dumps(node_tier)};"
        f"window.__RV_TIERS__ = {json.dumps(present)};"
    )
    # Real Lean source for a module node (tier-3), read live from lean_root.
    lean = proj.lean_source(node_id, node)
    lean_html = ""
    if lean:
        note = (f"<span class='rv-lean-trunc'>showing first "
                f"{len(lean['text'].splitlines())} of {lean['total']} lines</span>"
                if lean["truncated"] else "")
        # "contains sorry" badge: this module's Lean has a live sorry/admit/sorryAx
        # (its id is in the sorry set as a flagged *module*, not merely an ancestor),
        # so the packet flags the incomplete code beside the source. Empty/absent
        # lean_root ⇒ sorry_set is empty ⇒ no badge (unchanged behavior).
        sorry_note = ""
        if node_id in proj.sorry_set(nodes):
            sorry_note = ("<span class='rv-lean-sorry'>contains "
                          "<code>sorry</code> — not implemented yet</span>")
        lean_html = (
            "<section class='rv-lean'>"
            f"<h3 class='rv-lean-h'>Lean source <code>{_E(lean['rel'])}</code>"
            f"{note}{sorry_note}</h3>"
            f"<pre class='rv-lean-pre'><code>{_E(lean['text'])}</code></pre>"
            "</section>"
        )

    body = (
        "<div class='rv-packet'>"
        f"<section class='rv-packet-left'>{left}</section>"
        f"<section class='rv-packet-right'>{right}</section>"
        "</div>"
        + lean_html
        + _verdict_panel_html(node_id, scorecard)
    )
    return _page(f"node · {node_id}", body, boot)


def _node_meta_html(proj: Project, node_id: str, node: dict, scorecard: dict) -> str:
    kind = node.get("kind", "—")
    status = node.get("mathlib_status", "missing")
    parts = [f"<h3 class='rv-node-id'>{_E(node_id)}</h3>",
             f"<p class='rv-node-sub'>{_E(kind)} · mathlib_status: "
             f"{_E(status)}</p>"]

    refs = node.get("source_refs") or []
    if refs:
        parts.append("<div class='rv-card'><h4>source_refs (verbatim)</h4><ul>")
        for r in refs:
            # source_refs entries are verbatim citations: usually a plain string,
            # but tolerate a structured {file, location} dict too.
            if isinstance(r, dict):
                f = _E(str(r.get("file", "")))
                loc = _E(str(r.get("location", "")))
                parts.append(
                    f"<li><code>{f}</code> — {loc}</li>" if loc
                    else f"<li><code>{f}</code></li>")
            else:
                parts.append(f"<li>{_E(str(r))}</li>")
        parts.append("</ul></div>")

    decls = node.get("mathlib_declarations") or []
    if decls:
        parts.append("<div class='rv-card'><h4>mathlib_declarations</h4><ul>")
        for d in decls:
            parts.append(f"<li><code>{_E(d)}</code></li>")
        parts.append("</ul></div>")
    mfile = node.get("mathlib_file")
    if mfile:
        parts.append(f"<p class='rv-mfile'>file: <code>{_E(mfile)}</code></p>")

    kernel = proj.kernel_evidence(node_id)
    parts.append("<div class='rv-card rv-kernel'><h4>kernel evidence "
                 "(<code>#print axioms</code>)</h4>")
    if kernel:
        parts.append(f"<pre>{_E(kernel)}</pre>")
    else:
        parts.append("<p class='rv-note'>No <code>kernel/" + _E(node_id)
                     + ".txt</code> dump — run the reviewer/packet path to "
                     "generate kernel evidence.</p>")
    parts.append("</div>")

    # jury scorecard card
    ai = scorecard["ai"]
    parts.append("<div class='rv-card rv-scorecard'><h4>jury scorecard</h4>"
                 "<table class='rv-score-table'>"
                 "<tr><th>rubric</th><th>score</th><th>pass</th></tr>")
    for name, weight, thr in rm.RUBRICS:
        val = ai.get(name)
        cell = "—" if val is None else str(val)
        ok = "—" if val is None else ("✓" if val >= thr else "✗")
        parts.append(f"<tr><td>{_E(name)} <span class='rv-w'>×{weight}</span></td>"
                     f"<td>{cell}</td><td>{ok}</td></tr>")
    wt = ai.get("weighted")
    parts.append(f"<tr class='rv-score-total'><td>weighted</td>"
                 f"<td>{'—' if wt is None else wt}</td>"
                 f"<td>ai: {_E(str(ai.get('verdict') or '—'))}</td></tr>")
    parts.append("</table></div>")
    return "".join(parts)


def _verdict_panel_html(node_id: str, scorecard: dict) -> str:
    human = scorecard.get("human") or {}
    cur = human.get("verdict") or ""
    note = human.get("note") or ""
    score = human.get("score")
    eff = scorecard["effective"]
    src = scorecard["source"] or "—"
    return (
        "<form class='rv-verdict-panel' id='rv-verdict-form' "
        f"data-node='{_E(node_id)}'>"
        f"<h4>verdict panel <span class='rv-eff rv-{eff}'>effective: {_E(eff)}"
        f" ({_E(src)})</span></h4>"
        "<div class='rv-vp-row'>"
        + "".join(
            f"<label class='rv-radio'><input type='radio' name='verdict' "
            f"value='{v}'{' checked' if cur == v else ''}>{v}</label>"
            for v in rm.VERDICTS)
        + "</div>"
        "<div class='rv-vp-row'><label>score (0-5) "
        f"<input type='number' name='score' min='0' max='5' "
        f"value='{'' if score is None else _E(str(score))}'></label></div>"
        f"<div class='rv-vp-row'><label>note <textarea name='note'>{_E(note)}"
        "</textarea></label></div>"
        "<div class='rv-vp-row'><button type='submit'>save human verdict</button>"
        "<span class='rv-vp-status' id='rv-vp-status'></span></div>"
        "<p class='rv-note'>Human verdict is immutable — re-running the AI never "
        "overrides it.</p>"
        "</form>"
    )


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

def make_handler(proj: Project):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _send(self, code, body, ctype="text/html; charset=utf-8"):
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code, obj):
            self._send(code, json.dumps(obj), "application/json")

        # --- GET ---
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            if path == "/":
                qs = urllib.parse.parse_qs(parsed.query)
                # Home renders the flat tier-N graph (?tier=N, any tier present;
                # default = lowest tier present), OR a bounded local view:
                #   ?focus=<parentid>          → unit's children + immediate neighbors
                #   ?anchor=<nodeid>&radius=K  → neighborhood of one node (±K hops)
                # A flat too-large tier renders a placeholder + picker instead.
                tier = qs.get("tier", [None])[0]
                focus = qs.get("focus", [None])[0]
                anchor = qs.get("anchor", [None])[0]
                radius = qs.get("radius", [None])[0]
                return self._send(
                    200, render_home(proj, tier, focus, anchor, radius))
            if path == "/api/state":
                nodes = proj.nodes()
                return self._json(200, rm.compute_state(
                    nodes, proj.sidecar(), proj.sorry_set(nodes)))
            if path == "/api/dot":
                return self._api_dot(parsed)
            if path == "/api/agents":
                # Read-only live activity feed, recomputed from disk each call.
                return self._json(200, proj.agents())
            if path == "/api/dispatch":
                # The activity-panel superset: the dispatch palette, the current
                # queued tasks, and the existing live agents feed (unchanged). The
                # panel polls this one endpoint; /api/agents stays available too.
                return self._json(200, {
                    "palette": AGENT_PALETTE,
                    "queue": proj.task_queue(),
                    "live": proj.agents(),
                    "backend": _backend_payload(),
                })
            if path.startswith("/assets/"):
                return self._serve_asset(path[len("/assets/"):])
            if path.startswith("/cluster/"):
                cid = urllib.parse.unquote(path[len("/cluster/"):])
                return self._send(200, render_cluster(proj, cid))
            if path.startswith("/node/"):
                nid = urllib.parse.unquote(path[len("/node/"):])
                return self._send(200, render_node(proj, nid))
            return self._send(404, b"not found")

        def _api_dot(self, parsed):
            """GET /api/dot?tier=<N>&expand=id1,id2&focus=<p>&anchor=<n>&radius=K
            -> {dot, id_by_slug, kinds, tier, topo, neighborhood, anchor, too_large,
            nodes}.

            Lets the client re-render the graph with a transition (no full reload)
            after expanding/collapsing a node at the current tier. ``tier`` is any
            tier present in the graph (1, 2, 3, …); ``expand`` is a comma list of
            tier-N node ids to unroll in place into their tier-(N+1) children —
            unknown ids and leaves are ignored by the model.

            Mirrors the same **local-view** selection as ``render_home`` so a client
            re-render matches the server's first paint:

              * ``focus=<parentid>``         → ``only = neighborhood(children, ±1)``;
              * ``anchor=<nodeid>&radius=K``  → ``only = neighborhood({anchor}, ±K)``
                (K clamped 1..3);
              * flat **too-large** tier (> LARGE, no focus/anchor) → an empty
                placeholder DOT (never the full N-node graph) + ``too_large``/``nodes``
                so the client shows the picker, exactly as the home guard does.

            The payload mirrors the home bootstrap globals the client needs to repaint
            and re-wire clicks.
            """
            nodes = proj.nodes()
            sidecar = proj.sidecar()
            qs = urllib.parse.parse_qs(parsed.query)
            tier = _parse_tier(qs.get("tier", [None])[0], nodes)
            expand_raw = qs.get("expand", [""])[0]
            expanded = {c.strip() for c in expand_raw.split(",") if c.strip()}

            focus = qs.get("focus", [None])[0]
            anchor = qs.get("anchor", [None])[0]
            radius = _clamp_radius(qs.get("radius", [None])[0])
            focus_payload = _focus_payload(focus, nodes)
            anchor_payload = _anchor_payload(anchor, radius, nodes)

            only = None
            neighborhood_view = False
            too_large = None
            tier_nodes = None
            if focus_payload is not None:
                only = rm.neighborhood(set(focus_payload["members"]), nodes, tier,
                                       radius=1, cap=NB_CAP)
                neighborhood_view = True
            elif anchor_payload is not None:
                only = rm.neighborhood({anchor_payload["id"]}, nodes, tier, radius,
                                       cap=NB_CAP)
                neighborhood_view = True
            elif _tier_count(tier, nodes) > LARGE:
                # Flat too-large tier: never emit the full graph — placeholder + picker.
                too_large = {"tier": tier, "count": _tier_count(tier, nodes),
                             "threshold": LARGE}
                tier_nodes = _tier_nodes_payload(tier, nodes)

            if too_large is not None:
                dot = _placeholder_dot()
            else:
                # Live sorry set (ids + ancestors); empty without a lean_root, so a
                # re-render matches the home paint and is unchanged when inert.
                sorry_set = proj.sorry_set(nodes)
                dot = rm.recolor_dot(nodes, sidecar, tier=tier,
                                     expanded=expanded, only=only,
                                     sorry_set=sorry_set)
            slug_map = proj.slug_map()
            id_by_slug = {slug: nid for nid, slug in slug_map.items()}
            kinds = {nid: (node.get("kind") or "theorem")
                     for nid, node in nodes.items()}
            return self._json(200, {
                "dot": dot,
                "id_by_slug": id_by_slug,
                "kinds": kinds,
                "tier": tier,
                # Same tier-agnostic topology as the home boot, so a re-render after
                # an unroll keeps the client's parent/leaf + child→parent maps fresh.
                "topo": _topology(nodes),
                # Local-view / guard mirrors so a re-render matches the home paint.
                "focus": focus_payload,
                "neighborhood": neighborhood_view,
                "anchor": anchor_payload,
                "too_large": too_large,
                "nodes": tier_nodes,
            })

        def _serve_asset(self, name):
            # Only serve files under assets/review/ (no traversal).
            safe = Path(name).name
            f = _ASSETS_DIR / safe
            if not f.is_file():
                return self._send(404, b"asset not found")
            ctype = {
                ".css": "text/css; charset=utf-8",
                ".js": "application/javascript; charset=utf-8",
                ".svg": "image/svg+xml",
            }.get(f.suffix, "application/octet-stream")
            return self._send(200, f.read_bytes(), ctype)

        def _read_json_body(self):
            """Read + parse the request's JSON body, or (None) on bad JSON. Returns the
            parsed object, or the sentinel ``_BAD_JSON`` when the body is not valid JSON
            (so the caller can answer 400 rather than crash)."""
            length = int(self.headers.get("Content-Length", "0"))
            try:
                return json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return _BAD_JSON

        def _csrf_denied(self) -> Optional[str]:
            """Why this POST fails the CSRF gate (Host allowlist + token), or None."""
            deny = _host_denied(self.headers.get("Host"),
                                self.server.server_address[1])
            if deny:
                return deny
            if self.headers.get("X-Review-Token") != _API_TOKEN:
                return "missing or invalid X-Review-Token"
            return None

        # --- POST ---
        def do_POST(self):
            # CSRF gate on EVERY POST (GETs stay unauthenticated): loopback Host
            # + the per-process token the served page carries. A cross-origin
            # page can fire the request but can never read the token.
            deny = self._csrf_denied()
            if deny:
                return self._json(403, {"ok": False, "error": deny})
            path = urllib.parse.urlparse(self.path).path
            if path == "/api/request":
                return self._post_request()
            if path == "/api/request/cancel":
                return self._post_request_cancel()
            if path == "/api/backend":
                return self._post_backend()
            if path.startswith("/api/verdict/"):
                return self._post_verdict(path)
            return self._json(404, {"ok": False, "error": "not found"})

        def _post_backend(self):
            """POST /api/backend {backend} → persist the prover backend to the shared
            config (~/.autoform/config.json — the same file /autoform:set-backend uses),
            so the UI dropdown and the CLI stay in sync. 400 unless the backend is known.
            Returns {ok:true, backend:{current, options}}."""
            posted = self._read_json_body()
            if posted is _BAD_JSON:
                return self._json(400, {"ok": False, "error": "bad json"})
            backend = posted.get("backend")
            if backend not in BACKENDS:
                return self._json(400, {"ok": False,
                                        "error": f"unknown backend {backend!r}"})
            try:
                set_backend(backend)
            except Exception as err:  # pragma: no cover - disk/permission edge
                return self._json(500, {"ok": False, "error": str(err)})
            return self._json(200, {"ok": True, "backend": _backend_payload()})

        def _post_request(self):
            """POST /api/request {agent, node} → enqueue a dispatch request.

            400 unless ``agent`` is in AGENT_PALETTE AND ``node`` is in the graph.
            Otherwise append a queued task record (id = f"{agent}:{node}", stable) and
            DEDUPE — an identical agent+node already queued/running returns the existing
            queue unchanged, never a duplicate. Writes ONLY task_queue.json; never
            spawns an agent. Returns {ok:true, queue:[...]}.
            """
            posted = self._read_json_body()
            if posted is _BAD_JSON:
                return self._json(400, {"ok": False, "error": "bad json"})
            agent = posted.get("agent")
            node = posted.get("node")
            if agent not in _PALETTE_IDS:
                return self._json(400, {"ok": False,
                                        "error": f"unknown agent {agent!r}"})
            nodes = proj.nodes()
            if node not in nodes:
                return self._json(400, {"ok": False,
                                        "error": f"unknown node {node!r}"})

            # Load-mutate-save under the cross-process lock: the engine (runner /
            # dispatch_queue) mutates the same file, so an unlocked cycle here could
            # erase its just-landed claim/finish (or vice versa).
            with fslock.locked(proj.task_queue_path):
                queue = proj.task_queue()
                # Dedupe: an identical agent+node already queued or running blocks a
                # duplicate — return the existing queue untouched (idempotent enqueue).
                for t in queue:
                    if (t.get("agent") == agent and t.get("node") == node
                            and t.get("status") in _ACTIVE_STATUSES):
                        return self._json(200, {"ok": True, "queue": queue})

                node_label = nodes[node].get("name") or node
                now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                queue.append({
                    "id": f"{agent}:{node}",
                    "agent": agent,
                    "node": node,
                    "node_label": node_label,
                    "status": "queued",
                    "at": now,
                    "requested_by": "dashboard",
                })
                proj.write_task_queue(queue)
            return self._json(200, {"ok": True, "queue": proj.task_queue()})

        def _post_request_cancel(self):
            """POST /api/request/cancel {id} → remove a task, only if it is *queued*.

            A running task is never cancellable from the dashboard (status reflects the
            real orchestrator). Removes only an entry whose status == "queued". Writes
            only task_queue.json. Returns {ok:true, queue:[...]} (unchanged if the id is
            unknown or not queued).
            """
            posted = self._read_json_body()
            if posted is _BAD_JSON:
                return self._json(400, {"ok": False, "error": "bad json"})
            task_id = posted.get("id")
            with fslock.locked(proj.task_queue_path):   # see _post_request
                queue = proj.task_queue()
                kept = [t for t in queue
                        if not (t.get("id") == task_id and t.get("status") == "queued")]
                if len(kept) != len(queue):
                    proj.write_task_queue(kept)
                    kept = proj.task_queue()
            return self._json(200, {"ok": True, "queue": kept})

        def _post_verdict(self, path):
            node_id = urllib.parse.unquote(path[len("/api/verdict/"):])
            nodes = proj.nodes()
            if node_id not in nodes:
                return self._json(404, {"ok": False, "error": "unknown node"})
            posted = self._read_json_body()
            if posted is _BAD_JSON:
                return self._json(400, {"ok": False, "error": "bad json"})

            verdict = posted.get("verdict")
            if verdict not in rm.VERDICTS:
                return self._json(400, {"ok": False,
                                        "error": f"verdict must be one of "
                                                 f"{list(rm.VERDICTS)}"})
            score = posted.get("score")
            try:
                score = None if score in (None, "") else int(score)
            except (TypeError, ValueError):
                return self._json(400, {"ok": False, "error": "score not an int"})

            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            # Load-mutate-save under the cross-process lock: the runner's finalize()
            # rewrites the same sidecar, and an unlocked cycle here could lose either
            # its fresh ai slot or (far worse) this human verdict.
            with fslock.locked(proj.sidecar_path):
                sidecar = proj.sidecar()
                try:
                    updated = rm.apply_human_verdict(
                        sidecar, node_id, verdict, score,
                        posted.get("note", ""), posted.get("by", "reviewer"), now)
                except ValueError as exc:
                    return self._json(400, {"ok": False, "error": str(exc)})
                proj.write_sidecar(updated)

            # Recompute and return the delta the client needs to repaint. Thread the
            # live sorry set so the returned taint/frontier/coverage reflect incomplete
            # Lean too (empty without a lean_root ⇒ unchanged behavior).
            state = rm.compute_state(nodes, updated, proj.sorry_set(nodes))
            return self._json(200, {
                "ok": True,
                "node": node_id,
                "effective": rm.verdict_of(node_id, updated),
                "verdicts": state["verdicts"],
                "tainted": state["tainted"],
                "coverage": state["coverage"],
                "trust_frontier": state["trust_frontier"],
            })

    return Handler


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Local DAG review server (127.0.0.1)")
    ap.add_argument("--graph", type=Path, required=True, help="path to graph.json")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--open", action="store_true",
                    help="open the home screen in a browser")
    ap.add_argument("--lean-root", type=Path, default=None,
                    help="root of the Lean sources (module A.B.C -> "
                         "<lean-root>/A/B/C.lean); enables the Lean-source panel on "
                         "module packets. Overrides graph metadata.lean_root.")
    args = ap.parse_args(argv)

    graph_path = args.graph.resolve()
    if not graph_path.is_file():
        ap.error(f"graph.json not found: {graph_path}")
    proj = Project(graph_path)
    if args.lean_root is not None:
        proj.lean_root = args.lean_root.resolve()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(proj))
    url = f"http://127.0.0.1:{args.port}/"
    print(f"review server → {url}  (Ctrl-C to stop)")
    print(f"  graph:   {graph_path}")
    print(f"  sidecar: {proj.sidecar_path}")
    if not proj.blueprint_html.is_file():
        print(f"  note: blueprint not built at {proj.blueprint_html}\n"
              f"        node packets will show raw informal_content until "
              f"`review --view` builds it.")
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
