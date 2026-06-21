#!/usr/bin/env python3
"""Pure-compute review model for the DAG-native review surface.

This module holds **all** the review logic the server needs, with **no I/O side
effects** beyond reading the two source files (``graph.json`` + sidecar) and never
writing anything. Every function here is a pure function of (graph, sidecar):
load them once, then compute verdicts / taint / roll-ups / coverage / frontier and
a recolored DOT — the HTTP layer in ``serve_review.py`` only formats the output and
owns the single write (``review_status.json``).

Two encodings live side by side on the graph and must never be conflated:

  * **position** = ``mathlib_status`` — vertical lanes, ``in-mathlib`` at the
    *bottom* rising to ``missing`` at the *top*; dependencies flow upward.
  * **color** = the **trust state**: blue = already in Mathlib (reused, trusted
    by construction — not ours to review) / green = ours, reviewed clean / amber
    = ours, flagged / red = ours, rejected / grey = ours, unreviewed. A real
    defect (flagged/rejected verdict) overrides to amber/red even on a Mathlib
    node. The two encodings *reinforce* each other: a blue (in-Mathlib) node
    also sits in the in-Mathlib lane.

The sidecar (``review_status.json``) is the single source of truth for verdicts.
Schema::

    { "version": 1, "updated_at": "<iso>", "settings": {"dial": "on-demand"},
      "reviews": {
        "<node id>": {
          "ai":    {"faithfulness": 4, "proof_integrity": 2, "code_quality": 5,
                    "verdict": "rejected", "at": "<iso>"},
          "human": {"verdict": "clean|flagged|rejected", "score": 0-5,
                    "note": "", "by": "<user>", "at": "<iso>"}  // omitted until reviewed
    } } }

The graph is read with the same loader as ``export_blueprint.py`` (nodes as a dict
keyed by id, or a list), and the DOT recolor reuses that module's read-only node /
attribute emitters so a recolored graph is byte-compatible with the exported one —
only the color source changes (verdict instead of ``mathlib_status``).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# Reuse the proven, read-only graph loader + DOT emitters from the exporter so the
# recolored graph matches the exported one exactly (layout, shapes, edges) and only
# the *color* differs. We import lazily-safely: scripts/ is on sys.path when the
# server runs, but support running this module standalone too.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import export_blueprint as eb  # noqa: E402  (sys.path adjusted above)

# ---------------------------------------------------------------------------
# Palette — verdict -> (DOT border color, fill color). These are the
# *review* colors and deliberately differ from the exporter's mathlib_status colors.
# ---------------------------------------------------------------------------
PALETTE = {
    "paper": "#F7F4EE",
    "ink": "#1F1D1A",
    "accent": "#1A4B8C",
    "in_mathlib": "#2563B0",  # blue — already in Mathlib (reused, trusted)
    "clean": "#2F7D4F",
    "flagged": "#C08A1E",
    "rejected": "#C0392B",
    "sorry": "#7C3AED",  # violet — incomplete (Lean contains a sorry/admit/sorryAx)
    "grey": "#C9C2B4",  # unreviewed
}

# Color-state -> DOT colors. Fills are light tints; borders are the palette.
# "in_mathlib" (blue) is a trust state, not a verdict: a node already in Mathlib.
VERDICT_DOT = {
    "in_mathlib": {"color": PALETTE["in_mathlib"], "fill": "#DCE8F7"},
    "clean":      {"color": PALETTE["clean"],    "fill": "#D6EAD9"},
    "flagged":    {"color": PALETTE["flagged"],  "fill": "#F2E4C4"},
    "rejected":   {"color": PALETTE["rejected"], "fill": "#F1D2CE"},
    "sorry":      {"color": "#7C3AED",           "fill": "#ECE7FB"},
    "unreviewed": {"color": PALETTE["grey"],     "fill": "#ECE7DC"},
}

# mathlib_status values that mean a node is *fully* in Mathlib (=> blue, trusted).
# Canonical value is "exists"; tolerate the other spellings seen in the wild.
IN_MATHLIB_STATUSES = {"exists", "in-mathlib", "in_mathlib", "mathlib"}

# ---------------------------------------------------------------------------
# Jury rubrics — SINGLE SOURCE OF TRUTH: skills/eval-rubrics/references/*.json.
# The axis set, weights, pass thresholds and gating roles are READ FROM THOSE FILES,
# so the jury is modular: add a rubric file to add an axis, remove one to shrink the
# jury (down to a single reviewer), and everything downstream — the weighted score,
# the verdict gate, the parallel dispatcher — adapts with NO code change. A built-in
# fallback keeps this module usable (tests / missing files) without the rubric dir.
# ---------------------------------------------------------------------------
_RUBRIC_DIR = Path(__file__).resolve().parents[2] / "skills" / "eval-rubrics" / "references"

# The built-in three — used only if no rubric files are found.
_FALLBACK_RUBRICS: List[dict] = [
    {"name": "faithfulness",    "weight": 0.40, "pass_threshold": 4, "max_score": 5, "reject_at_or_below": 2},
    {"name": "proof_integrity", "weight": 0.40, "pass_threshold": 3, "max_score": 5, "reject_at_or_below": 2},
    {"name": "code_quality",    "weight": 0.20, "pass_threshold": 3, "max_score": 5, "verdict_ceiling": "flagged"},
]


def _spec(d: dict) -> dict:
    """Normalize one rubric dict into a jury spec. A ``verdict_ceiling`` (e.g.
    ``"flagged"``) marks a *style* axis that can NEVER reject; its absence makes the
    axis a *correctness* gate that rejects at/below ``reject_at_or_below`` (default 2)."""
    can_reject = d.get("verdict_ceiling") is None
    return {
        "name": d["name"],
        "weight": float(d.get("weight", 0.0)),
        "pass_threshold": int(d.get("pass_threshold", 3)),
        "max_score": int(d.get("max_score", 5)),
        "can_reject": can_reject,
        "reject_at_or_below": (int(d.get("reject_at_or_below", 2)) if can_reject else None),
        "reviewer": d.get("reviewer"),
    }


def _load_specs() -> List[dict]:
    """Read every rubric file once → ordered specs (heaviest axis first, ties by name).
    Falls back to the built-in three if the dir is absent/empty/unreadable."""
    raw: List[dict] = []
    try:
        for f in sorted(_RUBRIC_DIR.glob("*.json")):
            try:
                raw.append(json.loads(f.read_text()))
            except Exception:
                continue
    except Exception:
        raw = []
    specs = [_spec(d) for d in raw] or [_spec(d) for d in _FALLBACK_RUBRICS]
    specs.sort(key=lambda s: (-s["weight"], s["name"]))
    return specs


_SPECS: List[dict] = _load_specs()


def rubric_specs() -> List[dict]:
    """The ordered jury axis specs (loaded once at import from the rubric files)."""
    return _SPECS


def load_rubrics() -> dict:
    """Full rubric dicts (criteria, prompt_template, …) keyed by axis name — for the
    dispatcher to build judge prompts. Same files as the specs; empty if absent."""
    out: dict = {}
    try:
        for f in sorted(_RUBRIC_DIR.glob("*.json")):
            try:
                d = json.loads(f.read_text())
                out[d["name"]] = d
            except Exception:
                continue
    except Exception:
        pass
    return out


# Derived, in spec order — what the rest of the module + the dispatcher consume.
AXES: List[str] = [s["name"] for s in _SPECS]
RUBRICS: List[Tuple[str, float, int]] = [(s["name"], s["weight"], s["pass_threshold"]) for s in _SPECS]

VERDICTS = ("clean", "flagged", "rejected")
DIALS = ("on-demand", "targets", "full")


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------

def load_graph(path: Path) -> Tuple[Dict[str, dict], dict]:
    """Load graph.json -> (nodes_by_id, metadata) via the exporter's loader."""
    return eb.load_graph(Path(path))


def empty_sidecar() -> dict:
    """A fresh, valid sidecar envelope (default dial = on-demand)."""
    return {"version": 1, "updated_at": None,
            "settings": {"dial": "on-demand"}, "reviews": {}}


def _backup_corrupt_sidecar(p: Path, err: Exception) -> Optional[Path]:
    """Move a corrupt sidecar aside so its (irreplaceable) human verdicts are never
    silently overwritten by the next write. Returns the backup path, or None."""
    backup = p.with_name(p.name + ".corrupt")
    n = 1
    while backup.exists():
        backup = p.with_name(f"{p.name}.corrupt.{n}")
        n += 1
    try:
        p.rename(backup)
    except OSError:
        return None
    print(
        f"WARNING: {p} is corrupt ({err}). It has been preserved as {backup} and a "
        f"fresh sidecar will be created — any human verdicts are recoverable from the "
        f"backup, NOT lost.",
        file=sys.stderr,
    )
    return backup


def load_sidecar(path: Path) -> dict:
    """Load review_status.json, returning a fresh envelope if absent.

    Never raises on a missing file (the sidecar is runtime data, absent on first
    review). A *corrupt* file is preserved (renamed to ``<name>.corrupt``) and a loud
    warning is emitted before falling back to a fresh envelope — the sidecar holds the
    irreplaceable human verdicts, so it is never silently discarded.
    """
    p = Path(path)
    if not p.is_file():
        return empty_sidecar()
    try:
        text = p.read_text()
    except OSError as err:
        print(f"WARNING: could not read {p} ({err}); using a fresh sidecar for this "
              f"read (the file is left untouched).", file=sys.stderr)
        return empty_sidecar()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as err:
        _backup_corrupt_sidecar(p, err)
        return empty_sidecar()
    if not isinstance(data, dict):
        _backup_corrupt_sidecar(p, TypeError("sidecar root is not a JSON object"))
        return empty_sidecar()
    data.setdefault("version", 1)
    data.setdefault("settings", {})
    data["settings"].setdefault("dial", "on-demand")
    data.setdefault("reviews", {})
    if not isinstance(data["reviews"], dict):
        data["reviews"] = {}
    return data


def save_sidecar(path: Path, data: dict) -> None:
    """Atomically persist the sidecar (temp file + ``os.replace``) so an interrupted
    or concurrent write can never leave a half-written ``review_status.json``."""
    p = Path(path)
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, p)


def dial_of(sidecar: dict) -> str:
    """The current spec-generation dial; defaults to on-demand if unset/invalid."""
    d = (sidecar.get("settings") or {}).get("dial", "on-demand")
    return d if d in DIALS else "on-demand"


# ---------------------------------------------------------------------------
# live agent activity feed (read-only input, written by the orchestrator)
# ---------------------------------------------------------------------------

def _empty_agents() -> dict:
    """The default activity feed: an idle orchestrator and no running agents."""
    return {"orchestrator": {"state": "idle"}, "agents": []}


def load_agents(path: Path) -> dict:
    """Load ``agents_status.json`` (the live activity feed), never raising.

    The feed is a purely read-only input the orchestrator may write while a run is
    in flight; the review server only ever reads it. Absent file, unreadable file,
    corrupt JSON, or a non-object/oddly-shaped root all degrade gracefully to
    ``{"orchestrator": {"state": "idle"}, "agents": []}`` — the dashboard must never
    error just because no run is active. A well-formed feed is normalized so the
    caller can rely on an ``orchestrator`` dict (with a ``state``) and an ``agents``
    list always being present.
    """
    p = Path(path)
    if not p.is_file():
        return _empty_agents()
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return _empty_agents()
    if not isinstance(data, dict):
        return _empty_agents()

    orch = data.get("orchestrator")
    if not isinstance(orch, dict):
        orch = {"state": "idle"}
    else:
        orch = dict(orch)
        orch.setdefault("state", "idle")

    agents = data.get("agents")
    if not isinstance(agents, list):
        agents = []
    else:
        agents = [a for a in agents if isinstance(a, dict)]

    out = dict(data)
    out["orchestrator"] = orch
    out["agents"] = agents
    return out


# ---------------------------------------------------------------------------
# jury scoring -> verdict (Feature 6, threshold-gated, NOT the average)
# ---------------------------------------------------------------------------

def weighted_score(scores: dict) -> Optional[float]:
    """Weighted mean over the loaded rubric axes — default 0.40*faith + 0.40*integ + 0.20*qual (0-5), or None.

    Returns None if any rubric score is missing (so the caller can show "—").
    """
    vals = []
    for name, weight, _ in RUBRICS:
        v = scores.get(name)
        if v is None:
            return None
        vals.append(weight * float(v))
    return round(sum(vals), 2)


def jury_verdict(scores: dict) -> str:
    """Map rubric scores to a verdict, threshold-gated — GENERIC over whatever rubric
    set eval-rubrics defines (Feature 6). For the default three axes this reproduces
    the original behavior exactly (faith/integ <=2 reject; faith>=4 & integ>=3 &
    qual>=3 clean).

      * rejected — any *correctness* axis (no ``verdict_ceiling``) scored at or below
        its ``reject_at_or_below`` line. A *style* axis (verdict ceiling) never rejects.
      * clean    — every axis meets its ``pass_threshold``.
      * flagged  — anything else.

    Missing scores are treated conservatively as a fail (cannot be clean), and a
    missing correctness score does not trigger a rejection.
    """
    for s in _SPECS:
        if s["can_reject"]:
            v = scores.get(s["name"])
            if v is not None and v <= s["reject_at_or_below"]:
                return "rejected"
    if all(scores.get(s["name"]) is not None and scores[s["name"]] >= s["pass_threshold"]
           for s in _SPECS):
        return "clean"
    return "flagged"


# ---------------------------------------------------------------------------
# effective verdict (human immutable; human if present, else ai, else unreviewed)
# ---------------------------------------------------------------------------

def verdict_of(node_id: str, sidecar: dict) -> str:
    """The effective verdict for a node: human if present, else ai, else unreviewed.

    Human is immutable: re-running the AI only rewrites the ``ai`` slot, never
    overrides a recorded human verdict. Returns one of
    ``clean|flagged|rejected|unreviewed``.
    """
    rec = (sidecar.get("reviews") or {}).get(node_id)
    if not rec:
        return "unreviewed"
    human = rec.get("human")
    if human and human.get("verdict") in VERDICTS:
        return human["verdict"]
    ai = rec.get("ai")
    if ai and ai.get("verdict") in VERDICTS:
        return ai["verdict"]
    return "unreviewed"


def review_source(node_id: str, sidecar: dict) -> Optional[str]:
    """Which slot the effective verdict came from: 'human', 'ai', or None.

    Drives the dashed-ring (AI-only) vs solid-fill (human-confirmed) rendering.
    """
    rec = (sidecar.get("reviews") or {}).get(node_id)
    if not rec:
        return None
    human = rec.get("human")
    if human and human.get("verdict") in VERDICTS:
        return "human"
    ai = rec.get("ai")
    if ai and ai.get("verdict") in VERDICTS:
        return "ai"
    return None


# ---------------------------------------------------------------------------
# trust state — blue (in Mathlib) as a first-class color, distinct from verdict
# ---------------------------------------------------------------------------

def is_in_mathlib(node: dict) -> bool:
    """True when a node is *fully* in Mathlib (trusted by construction).

    Keyed on ``mathlib_status`` — canonical ``"exists"``, with the other common
    spellings tolerated. Such a node is reused, not ours to review, so it is
    neither "unreviewed" (grey) nor "we proved it" (green) — it is **blue**.
    """
    return (node or {}).get("mathlib_status") in IN_MATHLIB_STATUSES


def color_state(node: dict, effective_verdict: str, is_sorry: bool = False) -> str:
    """The **trust-state color** for a node: one of ``sorry`` (violet) /
    ``in_mathlib`` (blue) / ``clean`` (green) / ``flagged`` (amber) /
    ``rejected`` (red) / ``unreviewed`` (grey).

    Precedence (top wins):
      1. **sorry / not-implemented** (violet) — the Lean contains a
         ``sorry``/``admit``/``sorryAx``: the code is *incomplete*, the dominant
         fact, overriding any verdict and the blue in-Mathlib state.
      2. a real defect (flagged/rejected) shows even on a reuse — e.g. a wrong
         Mathlib lemma cited — so it overrides the blue.
      3. an in-Mathlib node is blue.
      4. a node still **missing** from Mathlib with no verdict yet has no real
         proof — in the plan model ``missing`` means "needs a statement *and*
         proof", so it is **not implemented**: violet, not grey "unreviewed".
      5. otherwise keep the effective verdict (clean -> green, else grey).

    ``is_sorry=False`` (the default) reproduces the original behavior exactly.
    """
    if is_sorry:
        return "sorry"
    if effective_verdict in ("rejected", "flagged"):
        return effective_verdict
    if is_in_mathlib(node):
        return "in_mathlib"
    if (node or {}).get("mathlib_status") == "missing" and effective_verdict == "unreviewed":
        return "sorry"  # planned / missing, no proof yet → "not implemented" (violet)
    return effective_verdict


def is_trusted(
    node_id: str,
    node: dict,
    sidecar: dict,
    sorry_set: Optional[Set[str]] = None,
) -> bool:
    """Whether a node is *trusted* for taint/frontier/coverage.

    Trusted = effective verdict ``clean`` (ours, reviewed clean) **OR** the node
    is in Mathlib (trusted by construction) and carries no defect verdict. A
    flagged/rejected node is never trusted, even if it is in Mathlib.

    A node in ``sorry_set`` (its Lean is incomplete) is **never trusted**,
    regardless of verdict or Mathlib status — incomplete code is an honest gap,
    not something a human can rely on end-to-end. ``sorry_set=None`` (the default)
    reproduces the original behavior exactly (empty set ⇒ no node is sorry).
    """
    if node_id in (sorry_set or set()):
        return False
    v = verdict_of(node_id, sidecar)
    if v == "clean":
        return True
    return is_in_mathlib(node) and v not in ("flagged", "rejected")


def node_scorecard(node_id: str, sidecar: dict) -> dict:
    """A flat scorecard for the packet/UI: per-rubric scores, weighted total,
    ai/human verdicts, the effective verdict, and its source."""
    rec = (sidecar.get("reviews") or {}).get(node_id) or {}
    ai = rec.get("ai") or {}
    human = rec.get("human") or {}
    return {
        "id": node_id,
        "ai": {
            **{ax: ai.get(ax) for ax in AXES},
            "weighted": weighted_score(ai),
            "verdict": ai.get("verdict"),
            "at": ai.get("at"),
        },
        "human": {
            "verdict": human.get("verdict"),
            "score": human.get("score"),
            "note": human.get("note"),
            "by": human.get("by"),
            "at": human.get("at"),
        } if human else None,
        "effective": verdict_of(node_id, sidecar),
        "source": review_source(node_id, sidecar),
    }


# ---------------------------------------------------------------------------
# taint — forward depends_on reachability from flagged/rejected nodes
# ---------------------------------------------------------------------------

def _dependents_index(nodes: Dict[str, dict]) -> Dict[str, List[str]]:
    """Build id -> [ids that depend ON it] (reverse of depends_on).

    A node X taints everything that (transitively) depends on X, so we walk the
    *reverse* of the ``depends_on`` edges: from a bad node out to its dependents.
    """
    rev: Dict[str, List[str]] = {nid: [] for nid in nodes}
    for nid, node in nodes.items():
        for dep in node.get("depends_on", []) or []:
            if dep in nodes and dep != nid:
                rev.setdefault(dep, []).append(nid)
    return rev


def tainted_set(
    nodes: Dict[str, dict],
    sidecar: dict,
    sorry_set: Optional[Set[str]] = None,
) -> Set[str]:
    """The set of node ids tainted by some flagged/rejected/sorry ancestor.

    A node is *tainted* if any node in its ``depends_on`` transitive closure is a
    taint source. Taint sources are ``flagged`` ∪ ``rejected`` (defect verdicts)
    ∪ ``sorry_set`` (incomplete Lean): a node resting on incomplete code is itself
    not trustworthy end-to-end, exactly like one resting on a rejected overclaim.
    Computed live, never stored. The source nodes themselves are NOT in the tainted
    set (they are the source, not victims) — they carry their own color; taint marks
    the *downstream* trust damage.

    Only flagged/rejected/sorry nodes taint: an ``in_mathlib`` (blue) node is trusted
    by construction and never seeds taint. ``sorry_set=None`` (the default) reproduces
    the original behavior exactly (no sorry sources, only flagged/rejected taint).
    """
    sorry_set = sorry_set or set()
    rev = _dependents_index(nodes)
    bad = [nid for nid in nodes
           if verdict_of(nid, sidecar) in ("flagged", "rejected")
           or nid in sorry_set]

    tainted: Set[str] = set()
    stack = list(bad)
    while stack:
        cur = stack.pop()
        for dep in rev.get(cur, []):
            if dep not in tainted:
                tainted.add(dep)
                stack.append(dep)
    # A bad node that is itself downstream of another bad node stays colored by its
    # own verdict, so drop the originating bad nodes from the hatched set unless they
    # are tainted by a *different* upstream bad node (which the walk already added).
    return tainted


# ---------------------------------------------------------------------------
# tier topology — direct children one tier down, tiers present, has-children
# ---------------------------------------------------------------------------

def child_ids(parent_id: str, nodes: Dict[str, dict]) -> List[str]:
    """The direct children of ``parent_id`` one tier *down*, sorted.

    A child of a node at tier *N* is a node at tier *N+1* whose ``parent`` is
    ``parent_id``. Generalizes ``_tier2_children`` (a tier-1 cluster's tier-2
    children) to any tier, so a tier-2 statement's tier-3 declarations are found
    the same way. Returns ``[]`` for an unknown parent or a leaf.
    """
    parent = nodes.get(parent_id)
    if parent is None:
        return []
    want = eb.node_tier(parent) + 1
    return sorted(
        nid for nid, node in nodes.items()
        if eb.node_tier(node) == want and node.get("parent") == parent_id
    )


def has_children(node_id: str, nodes: Dict[str, dict]) -> bool:
    """Whether ``node_id`` has any direct children one tier down (vs a leaf)."""
    return bool(child_ids(node_id, nodes))


def tiers_present(nodes: Dict[str, dict]) -> List[int]:
    """The sorted, distinct tiers that actually occur in the graph.

    Drives the tier toggle (it lists exactly the tiers present) and the default
    home tier (the lowest present). E.g. a graph with clusters + statements +
    declarations returns ``[1, 2, 3]``.
    """
    return sorted({eb.node_tier(node) for node in nodes.values()})


# ---------------------------------------------------------------------------
# bounded local neighborhood — BFS by hop over depends_on, BOTH directions
# ---------------------------------------------------------------------------

def neighborhood(
    anchors,
    nodes: Dict[str, dict],
    tier: int,
    radius: int = 1,
    cap: int = 60,
) -> Set[str]:
    """The bounded set of **tier-`tier`** node ids within ``radius`` hops of
    ``anchors``, following ``depends_on`` in **both directions** (a node's own deps
    ∪ the nodes that depend on it), with the anchors themselves always included.

    This is what powers the *local view*: instead of rendering a whole too-large
    tier (e.g. 226 tier-3 modules), we render only a small subgraph around a node or
    a unit's children. The walk is restricted to a single tier — only tier-`tier`
    nodes are ever traversed or returned — and is a true breadth-first expansion by
    hop, so "closest first" is well defined.

    Bounding (faithful + fast):

      * BFS by hop from the anchor frontier; each hop adds the tier-`tier` neighbors
        (deps ∪ dependents) of the current frontier not yet seen.
      * **Anchors are always in the result**, even past ``cap`` — the thing you asked
        to center on is never dropped (anchors are added first, before any cap trim).
      * If a hop would push the total past ``cap``, only as many of that hop's new
        nodes as fit are kept (in stable sorted order) and the walk **stops** — the
        result never exceeds ``cap``. Closer nodes are therefore always preferred
        over farther ones (a full nearer hop is admitted before any farther hop).

    Returns a ``set`` (membership is what callers need); the *stable order* governs
    only which nodes survive the cap, not the return type. Unknown anchors and
    anchors of the wrong tier contribute nothing (an off-tier or missing anchor
    simply seeds no frontier), so an all-unknown anchor set yields ``set()``.
    """
    if radius < 0:
        radius = 0
    if cap < 0:
        cap = 0

    # Restrict the universe to this tier; the walk never leaves it.
    tier_ids = {nid for nid, node in nodes.items() if eb.node_tier(node) == tier}

    # Forward (deps) + reverse (dependents) adjacency, both confined to this tier.
    deps: Dict[str, Set[str]] = {nid: set() for nid in tier_ids}
    rdeps: Dict[str, Set[str]] = {nid: set() for nid in tier_ids}
    for nid in tier_ids:
        for dep in nodes[nid].get("depends_on", []) or []:
            if dep in tier_ids and dep != nid:
                deps[nid].add(dep)      # nid -> dep (nid depends on dep)
                rdeps[dep].add(nid)     # dep <- nid (nid is a dependent of dep)

    # Seed: only anchors that are real tier-`tier` nodes. Anchors are always kept,
    # even if there are more than `cap` of them (caller asked to center on them).
    seed = sorted(a for a in anchors if a in tier_ids)
    result: Set[str] = set(seed)
    if not seed or radius == 0:
        return result

    frontier = list(seed)
    for _hop in range(radius):
        # Gather this hop's new tier-`tier` neighbors (deps ∪ dependents), stable.
        nxt: Set[str] = set()
        for cur in frontier:
            nxt |= deps.get(cur, set())
            nxt |= rdeps.get(cur, set())
        new = sorted(n for n in nxt if n not in result)
        if not new:
            break
        room = cap - len(result)
        if room <= 0:
            break
        if len(new) > room:
            # This hop overflows the cap: keep the closest `room` (stable order)
            # and stop — farther hops are never admitted before a nearer one.
            result.update(new[:room])
            break
        result.update(new)
        frontier = new
    return result


# ---------------------------------------------------------------------------
# tier roll-up (any tier — a node's roll-up over its direct children)
# ---------------------------------------------------------------------------

def _tier2_children(cluster_id: str, nodes: Dict[str, dict]) -> List[str]:
    """The tier-2 node ids whose parent is this tier-1 cluster, sorted.

    Back-compat alias kept for tier-1 callers/tests; identical to
    ``child_ids`` for a tier-1 parent.
    """
    return child_ids(cluster_id, nodes)


def cluster_rollup(cluster_id: str, nodes: Dict[str, dict], sidecar: dict) -> dict:
    """Roll a tier-1 cluster up from its tier-2 children.

    A cluster is **clean only if every** tier-2 child is clean; any flagged or
    rejected child makes the cluster flagged (rejected propagates as flagged at the
    cluster level — a cluster is never "rejected", only flagged, per the design's
    roll-up rule: any flagged/rejected child => cluster flagged). A cluster with no
    reviewed children is unreviewed.
    """
    children = _tier2_children(cluster_id, nodes)
    child_verdicts = {c: verdict_of(c, sidecar) for c in children}

    counts = {"clean": 0, "flagged": 0, "rejected": 0, "unreviewed": 0}
    for v in child_verdicts.values():
        counts[v] = counts.get(v, 0) + 1

    if not children:
        rollup = "unreviewed"
    elif counts["flagged"] or counts["rejected"]:
        rollup = "flagged"
    elif counts["clean"] == len(children):
        rollup = "clean"
    else:
        # some clean, rest unreviewed -> not yet fully clean
        rollup = "unreviewed"

    return {
        "id": cluster_id,
        "children": children,
        "child_verdicts": child_verdicts,
        "counts": counts,
        "verdict": rollup,
    }


def rollup_color(parent_id: str, nodes: Dict[str, dict], sidecar: dict) -> str:
    """Trust color for *any* parent, rolled up from its direct children's
    ``color_state`` (so in-Mathlib reuse rolls up to blue). Works at every tier: a
    tier-1 cluster rolls up its tier-2 children, a tier-2 statement rolls up its
    tier-3 declarations, etc. Any flagged/rejected child => flagged; all children in
    Mathlib => blue; all trusted (clean or in-Mathlib) => clean; else unreviewed.

    A leaf (no children) returns ``unreviewed`` — a parentless roll-up is undefined,
    so callers should color a true leaf by its own ``color_state`` instead.
    """
    children = child_ids(parent_id, nodes)
    states = [color_state(nodes[c], verdict_of(c, sidecar))
              for c in children if c in nodes]
    if not states:
        return "unreviewed"
    if any(s in ("rejected", "flagged") for s in states):
        return "flagged"
    if all(s == "in_mathlib" for s in states):
        return "in_mathlib"
    if all(s in ("clean", "in_mathlib") for s in states):
        return "clean"
    return "unreviewed"


# Back-compat alias: a tier-1 cluster's roll-up color is just ``rollup_color`` of the
# cluster id. Existing callers/tests use ``cluster_color``; keep it pointing here.
def cluster_color(cluster_id: str, nodes: Dict[str, dict], sidecar: dict) -> str:
    """Alias for ``rollup_color`` — a tier-1 cluster's roll-up color over its
    tier-2 children. Retained so existing tier-1 callers/tests keep working."""
    return rollup_color(cluster_id, nodes, sidecar)


def rollup_source(parent_id: str, nodes: Dict[str, dict], sidecar: dict) -> str:
    """Solid-vs-dashed for a *collapsed parent* node. A parent is drawn **solid**
    ("human") only if every child's trust is vouched — each child is either in-Mathlib
    (blue, trusted by construction) or human-confirmed. If any child's status rests on
    an AI-only review (or is unreviewed), the parent is **dashed** ("ai" / provisional),
    so a cluster of only AI-reviewed nodes never looks human-vouched."""
    kids = child_ids(parent_id, nodes)
    if not kids:
        return "ai"
    for c in kids:
        if is_in_mathlib(nodes.get(c, {})):
            continue
        if review_source(c, sidecar) == "human":
            continue
        return "ai"
    return "human"


# ---------------------------------------------------------------------------
# coverage + trust frontier
# ---------------------------------------------------------------------------

def coverage(
    nodes: Dict[str, dict],
    sidecar: dict,
    sorry_set: Optional[Set[str]] = None,
) -> dict:
    """Review coverage over tier-2 nodes: how many have any effective verdict.

    Coverage is the header progress bar: reviewed / total tier-2. "Reviewed" means
    a node has an effective verdict (ai or human), i.e. it is not ``unreviewed``.

    ``sorry_set`` (default empty) is threaded into ``is_trusted`` so a node with
    incomplete Lean is not counted as trusted. ``sorry_set=None`` reproduces the
    original behavior exactly.
    """
    sorry_set = sorry_set or set()
    tier2 = [nid for nid, node in nodes.items() if eb.node_tier(node) == 2]
    reviewed = [nid for nid in tier2 if verdict_of(nid, sidecar) != "unreviewed"]
    human = [nid for nid in tier2 if review_source(nid, sidecar) == "human"]
    in_mathlib = [nid for nid in tier2 if is_in_mathlib(nodes[nid])]
    trusted = [nid for nid in tier2
               if is_trusted(nid, nodes[nid], sidecar, sorry_set)]
    total = len(tier2)
    return {
        "total": total,
        "reviewed": len(reviewed),
        "human_confirmed": len(human),
        "in_mathlib": len(in_mathlib),
        "trusted": len(trusted),
        "fraction": (len(reviewed) / total) if total else 0.0,
    }


def trust_frontier(
    nodes: Dict[str, dict],
    sidecar: dict,
    sorry_set: Optional[Set[str]] = None,
) -> List[str]:
    """The sink nodes (top-level results) resting on a fully-**trusted** closure.

    A sink is a tier-2 node that nothing else (in tier 2) depends on. A sink is on
    the trust frontier when it AND its entire ``depends_on`` closure are all
    *trusted* — each node is either reviewed clean or already in Mathlib (with no
    flagged/rejected defect). A blue (in-Mathlib) node therefore counts toward the
    frontier exactly like a clean one. These are the results a human can currently
    trust end-to-end. Sorted for determinism.

    ``sorry_set`` (default empty) is threaded into ``is_trusted`` so a sink whose
    closure touches incomplete Lean is *off* the frontier. ``sorry_set=None``
    reproduces the original behavior exactly.
    """
    sorry_set = sorry_set or set()
    tier2 = {nid: node for nid, node in nodes.items() if eb.node_tier(node) == 2}
    rev = _dependents_index(tier2)
    sinks = [nid for nid in tier2 if not rev.get(nid)]

    frontier: List[str] = []
    for sink in sinks:
        closure = _closure(sink, tier2)
        if all(is_trusted(nid, tier2[nid], sidecar, sorry_set) for nid in closure):
            frontier.append(sink)
    return sorted(frontier)


def _closure(start: str, nodes: Dict[str, dict]) -> Set[str]:
    """The node + its full within-tier depends_on transitive closure."""
    seen: Set[str] = {start}
    stack = [start]
    while stack:
        cur = stack.pop()
        for dep in nodes.get(cur, {}).get("depends_on", []) or []:
            if dep in nodes and dep not in seen:
                seen.add(dep)
                stack.append(dep)
    return seen


# ---------------------------------------------------------------------------
# DOT recolor — reuse the exporter's read-only node/attr emitters
# ---------------------------------------------------------------------------

def _verdict_node_dot(
    nid: str,
    node: dict,
    slug: str,
    verdict: str,
    source: Optional[str],
    tainted: bool,
    state_override: Optional[str] = None,
    is_sorry: bool = False,
) -> str:
    """Emit one DOT node line colored by the **trust state** (color_state), not the
    raw verdict.

    Encodings:
      * fill/border color = color_state (sorry=violet / in_mathlib=blue / clean=green /
        flagged=amber / rejected=red / unreviewed=grey);
      * sorry node         => **violet, SOLID** (a code fact — the Lean is incomplete —
        not an AI-only opinion, so no dashed ring and never the blue path), class
        ``rv-sorry``. Takes precedence over every verdict and over in_mathlib;
      * in_mathlib node    => **solid** (trusted by construction — no AI-only ring,
        never hatched), even when no human has reviewed it;
      * AI-only node       => dashed border ring (style=dashed);
      * human-confirmed    => solid filled;
      * tainted node       => 45-degree hatch overlay (style includes "diagonals"
        + a striped look approximated with style=striped fill, plus a data attr the
        client CSS uses for an exact 45deg hatch on the SVG).
    Shape still follows ``kind`` (box for definitions) exactly as the exporter does,
    so layout is identical to the exported graph.

    ``is_sorry=False`` (the default) reproduces the original behavior exactly.
    """
    kind = (node.get("kind") or "theorem").lower()
    shape = "box" if kind in eb.DEFINITION_KINDS else "ellipse"

    # sorry is the dominant fact: violet, solid, never the blue path — it overrides
    # any state_override (roll-up / verdict) coming from the caller.
    if is_sorry:
        state = "sorry"
    elif state_override is not None:
        state = state_override
    else:
        state = color_state(node, verdict)
    blue = state == "in_mathlib"

    colors = VERDICT_DOT.get(state, VERDICT_DOT["unreviewed"])
    attrs: List[str] = [
        f'label="{eb._dot_escape(nid)}"',
        f'color="{colors["color"]}"',
        f'fillcolor="{colors["fill"]}"',
        f"shape={shape}",
    ]

    # style: filled always. A sorry node is a code fact (the Lean is incomplete):
    # solid, no AI-only ring (its incompleteness does not depend on any review) and
    # never the blue path. A blue (in-Mathlib) node is trusted by construction:
    # solid, no AI-only ring and never hatched. Otherwise: dashed = AI-only ring,
    # "diagonals" hints taint.
    styles = ["filled"]
    if not is_sorry and not blue and source != "human":
        # AI-only (or unreviewed) -> dashed ring marks "provisional / unvouched".
        styles.append("dashed")
    if tainted and not is_sorry and not blue:
        # graphviz "diagonals" decorates the node; the client overlays a true 45deg
        # hatch via the class we tag below, so this is a graceful fallback.
        styles.append("diagonals")
    attrs.append(f'style="{",".join(styles)}"')

    # A class the client SVG post-processor keys on for the exact hatch + ring.
    # Sorry nodes are violet + solid (class rv-sorry); blue nodes are solid + never
    # tainted/AI-only-styled (trusted by construction).
    if is_sorry:
        klass = "rv-sorry rv-solid"
    elif blue:
        klass = "rv-in_mathlib rv-solid"
    else:
        klass = f"rv-{state}" + (" rv-tainted" if tainted else "") + (
            " rv-aionly" if source != "human" else " rv-human")
    attrs.append(f'class="{klass}"')

    return f'  "{slug}" [{", ".join(attrs)}];'


def transitive_reduction(succ: Dict[str, Set[str]]) -> Dict[str, Set[str]]:
    """Transitive reduction of a DAG given as an adjacency map ``succ`` (u -> direct
    successors of u). Returns a NEW adjacency map keeping only the non-redundant
    edges: drop ``u -> v`` exactly when some *other* direct successor ``w`` of ``u``
    (``w != v``) already reaches ``v`` (``v`` is in the reachability closure of
    ``w``). The reachable closure is computed by memoised DFS.

    DISPLAY-ONLY helper for the review render: it reduces the *drawn* edge set so a
    redundant ``u -> v`` (already implied by ``u -> w -> ... -> v``) is not painted.
    It is NEVER applied to the dependency semantics — ``compute_state`` / taint /
    ``trust_frontier`` / coverage keep using the full ``depends_on`` graph, so a
    reduced-away edge is still a real dependency for trust propagation.

    Acyclicity: the within-tier dependency graph is acyclic. If a cycle is present
    we must *never* drop an edge ``u -> v`` when ``u`` is itself reachable from ``v``
    (that would silently delete a real arc of the cycle), so such edges are always
    kept. Concretely, when testing whether ``w`` reaches ``v`` we ignore any ``w``
    that is reachable back from ``v`` (it sits on a cycle with ``v``), and we never
    drop ``u -> v`` while computing ``u``'s own reachability — the closure used to
    justify a drop excludes the edge under test.
    """
    nodes = set(succ)
    for vs in succ.values():
        nodes |= set(vs)

    # Memoised reachability closure: reach(x) = all nodes reachable from x following
    # one or more edges (x itself NOT included). DFS with a recursion guard so a cycle
    # cannot loop forever; on a cycle every member ends up reachable from every other.
    _reach: Dict[str, Set[str]] = {}

    def reach(x: str) -> Set[str]:
        cached = _reach.get(x)
        if cached is not None:
            return cached
        acc: Set[str] = set()
        _reach[x] = acc  # publish early so a back-edge on a cycle sees a (growing) set
        for y in succ.get(x, ()):  # direct successors
            acc.add(y)
            acc |= reach(y)
        return acc

    reduced: Dict[str, Set[str]] = {}
    for u in succ:
        outs = succ.get(u, set())
        kept: Set[str] = set()
        for v in outs:
            redundant = False
            for w in outs:
                if w == v or w == u:
                    continue
                # Cycle guard: if v reaches w, then w is on a cycle with v; a path
                # u -> w -> ... -> v would ride that cycle, so it does not justify
                # dropping the direct u -> v. Skip such w.
                if w in reach(v):
                    continue
                if v in reach(w):
                    redundant = True
                    break
            if not redundant:
                kept.add(v)
        reduced[u] = kept
    return reduced


def _recolor_tier_dot(
    nodes: Dict[str, dict],
    sidecar: dict,
    sub: Dict[str, dict],
    ids: Set[str],
    name_to_slug: Dict[str, str],
    lines: List[str],
    tier: int,
    expanded: Set[str],
    sorry_set: Optional[Set[str]] = None,
) -> str:
    """Emit the tier-*N* overview DOT with in-place expansion of any tier-*N* node
    into its tier-*(N+1)* children. Generalizes the old tier-1-only routine.

    For each node at ``tier``:

    * **Leaf** (no children at tier+1) → a single node colored by its own
      ``color_state`` (taint / AI-only ring honoured), exactly as a flat tier graph.
    * **Collapsed** parent (has children, id ∉ ``expanded``) → a single node colored
      by ``rollup_color`` over its direct children (clickable). Cross edges that
      touch it are lifted onto this node.
    * **Expanded** parent (id ∈ ``expanded``) → a ``subgraph cluster_<slug>`` box
      whose members are its tier-(N+1) children colored by their own ``color_state``,
      with the intra-parent ``depends_on`` edges drawn dashed *inside* the box.

    **Cross edges** (the repr trick) at the *current* tier: edges come from two
    sources, deduped together —

      * the tier-N nodes' own direct ``depends_on`` (tier-N → tier-N), mapped to
        node slugs; and
      * the tier-(N+1) children's ``depends_on`` lifted via ``repr``: for a child
        ``x``, ``repr(x) = slug(x)`` when its parent ∈ ``expanded`` (the box is open
        so the child is a real node) else ``slug(parent(x))`` (the collapsed parent
        node). Emit ``repr(d) -> repr(n)`` when the two slugs differ.

    An edge fully inside one expanded box (both children share an expanded parent)
    is emitted by that subgraph, never here.

    ``sorry_set`` (default empty) is the set of node ids whose Lean is incomplete: a
    node in it is drawn violet/solid (class ``rv-sorry``) and seeds taint, overriding
    its verdict/roll-up color. A parent unit/cluster is already in ``sorry_set`` (the
    server propagates a sorry to its ancestors), so its collapsed node goes violet
    over the roll-up. ``sorry_set=None`` reproduces the original behavior exactly.
    """
    sorry_set = sorry_set or set()
    expanded = {pid for pid in expanded if pid in ids and has_children(pid, nodes)}
    tainted = tainted_set(nodes, sidecar, sorry_set)

    # --- leaves + collapsed parents: one node each ---
    for nid in sorted(sub):
        if nid in expanded:
            continue
        if has_children(nid, nodes):
            own = verdict_of(nid, sidecar)
            if own != "unreviewed":
                # The node was itself reviewed → its OWN verdict wins over the roll-up.
                # A unit judged clean shows green even while its child modules are still
                # unreviewed; taint still hatches it if a defect sits in its closure.
                lines.append(_verdict_node_dot(
                    nid, sub[nid], name_to_slug[nid],
                    own, review_source(nid, sidecar), nid in tainted,
                    is_sorry=nid in sorry_set,
                ))
            else:
                # No own verdict (e.g. a cluster) → colored by the roll-up over children.
                lines.append(_verdict_node_dot(
                    nid, sub[nid], name_to_slug[nid],
                    "unreviewed", rollup_source(nid, nodes, sidecar), False,
                    state_override=rollup_color(nid, nodes, sidecar),
                    is_sorry=nid in sorry_set,
                ))
        else:
            # leaf → colored by its own trust state (taint / AI-only ring honoured)
            lines.append(_verdict_node_dot(
                nid, sub[nid], name_to_slug[nid],
                verdict_of(nid, sidecar),
                review_source(nid, sidecar),
                nid in tainted,
                is_sorry=nid in sorry_set,
            ))

    # --- expanded parents: a subgraph box per parent with its tier-(N+1) children ---
    for pid in sorted(expanded):
        children = child_ids(pid, nodes)
        label = sub[pid].get("name") or pid
        lines.append(f'  subgraph cluster_{name_to_slug[pid]} {{')
        lines.append(f'    label="{eb._dot_escape(label)}";')
        lines.append('    style="filled,rounded";')
        lines.append('    color="#C9C2B4";')
        lines.append('    fillcolor="#FBF9F4";')  # light roll-up tint background
        lines.append(f'    fontname="{eb.GRAPH_STYLE["font_name"]}";')
        for nid in children:
            line = _verdict_node_dot(
                nid, nodes[nid], name_to_slug[nid],
                verdict_of(nid, sidecar),
                review_source(nid, sidecar),
                nid in tainted,
                is_sorry=nid in sorry_set,
            )
            lines.append("  " + line)  # indent into the subgraph
        # intra-parent dashed edges (both endpoints among this parent's children)
        intra: List[Tuple[str, str]] = []
        cset = set(children)
        for nid in children:
            for dep in nodes[nid].get("depends_on", []) or []:
                if dep in cset and dep != nid:
                    intra.append((name_to_slug[dep], name_to_slug[nid]))
        for s, t in sorted(set(intra)):
            lines.append(f'    "{s}" -> "{t}" [style=dashed];')
        lines.append("  }")

    # --- cross edges at the current tier (deduped) ---
    cedges: Set[Tuple[str, str]] = set()

    # (a) tier-N nodes' own direct depends_on (e.g. a tier-2 statement depending on
    #     another tier-2 statement). Both endpoints must be tier-N nodes in view AND
    #     still drawn as bare nodes — an *expanded* endpoint is a subgraph box, not a
    #     node, so its bare slug must never appear in an edge (that would spawn a
    #     phantom node). When a node is expanded its real connectivity is shown by
    #     its children's own deps (b), so we simply skip a direct edge touching it.
    for nid in sub:
        if nid in expanded:
            continue
        for dep in sub[nid].get("depends_on", []) or []:
            if dep in ids and dep != nid and dep not in expanded:
                s, t = name_to_slug[dep], name_to_slug[nid]
                if s != t:
                    cedges.add((s, t))

    # (b) tier-(N+1) children's depends_on, lifted via the repr trick. A child maps
    #     to itself when its parent box is open, else to its (collapsed) tier-N
    #     parent node. This is what surfaces cross-parent structure when the parents
    #     themselves carry no direct depends_on.
    parent = {nid: node.get("parent") for nid, node in nodes.items()}
    want_child = tier + 1

    def _repr(x: str) -> Optional[str]:
        px = parent.get(x)
        if px in expanded:
            return name_to_slug[x]          # child node (its parent box is open)
        if px in ids:
            return name_to_slug[px]         # the collapsed tier-N parent node
        return None                          # parentless / out-of-view: skip

    for nid, node in nodes.items():
        if eb.node_tier(node) != want_child:
            continue
        rn = _repr(nid)
        if rn is None:
            continue
        for dep in node.get("depends_on", []) or []:
            rd = _repr(dep)
            if rd is None or rd == rn:
                continue
            cedges.add((rd, rn))

    # Transitive reduction of the DEDUPED emitted edge set (display-only): a pair
    # (s, t) means the drawn arc ``s -> t``. Build the successor map over exactly the
    # pairs we are about to draw — flat tier-N deps, only=-filtered deps, and the
    # expanded/lifted repr-trick deps all live in `cedges` together — reduce it, and
    # emit only the surviving non-redundant arcs. This changes only what is *drawn*;
    # taint / frontier / coverage still run over the full `depends_on` graph.
    succ: Dict[str, Set[str]] = {}
    for s, t in cedges:
        succ.setdefault(s, set()).add(t)
    reduced = transitive_reduction(succ)
    drawn: Set[Tuple[str, str]] = {
        (s, t) for s, ts in reduced.items() for t in ts}
    for s, t in sorted(drawn):
        lines.append(f'  "{s}" -> "{t}" [style=dashed];')

    lines.append("}")
    return "\n".join(lines)


def recolor_dot(
    nodes: Dict[str, dict],
    sidecar: dict,
    tier: int = 2,
    expanded: Optional[Set[str]] = None,
    only: Optional[Set[str]] = None,
    sorry_set: Optional[Set[str]] = None,
) -> str:
    """Build a DOT digraph for the given tier, recolored by effective verdict, with
    in-place expansion of any tier-*N* node into its tier-*(N+1)* children.

    Reuses the exporter's ``_graph_attr_lines`` (shared graph/node/edge attrs) and
    slug map, so the recolored graph is laid out identically to the exported one —
    only the node colors/styles change (verdict instead of mathlib_status) plus the
    dashed-ring / hatch encodings. Within-tier ``depends_on`` edges are emitted
    dashed.

    ``expanded`` is the set of tier-*N* node ids to **unroll in place** and now works
    at **any tier** (tier-1 clusters → tier-2 statements; tier-2 statements → tier-3
    declarations; …). For each node at ``tier``:

    * a **leaf** (no tier-(N+1) children) is a single node colored by its own
      ``color_state``;
    * a **collapsed** parent (has children, not in ``expanded``) is a single node
      colored by ``rollup_color`` over its direct children, with cross edges lifted
      onto it;
    * an **expanded** parent is a graphviz ``subgraph cluster_<slug> { label=...;
      <tier-(N+1) children colored by color_state>; intra-parent dashed edges }``.

    Cross edges use the *repr* trick at the current tier (each tier-N dependency
    ``d -> n`` mapped to its node slugs, deduped). Edges inside an expanded box are
    tier-(N+1) deps and are emitted by that subgraph, not here.

    ``only`` (a *local view*): when given, render **only** the tier-`tier` nodes in
    that set and the ``depends_on`` edges **between** them (a bounded subgraph, e.g.
    a ``neighborhood``). Same coloring, encodings, expansion and compact-layout rules
    apply — the only change is that ``sub``/``ids`` (the universe of drawn tier-N
    nodes) is filtered to ``only``, so every emitted edge already has both endpoints
    in ``only`` and no out-of-view node or edge can leak in. ``only=None`` keeps the
    full-tier behavior unchanged.

    ``sorry_set`` (a set of node ids whose Lean is incomplete) is threaded through to
    ``_recolor_tier_dot``: each drawn tier-N node ``nid`` is emitted with
    ``is_sorry=nid in sorry_set`` so a sorry node renders violet/solid (class
    ``rv-sorry``) and seeds taint. ``sorry_set=None`` (the default) reproduces the
    original behavior exactly.
    """
    name_to_slug = eb.build_slug_map(nodes)
    sub = {nid: node for nid, node in nodes.items() if eb.node_tier(node) == tier}
    if only is not None:
        # Local view: restrict the drawn tier-N universe to `only`. Every node line
        # and every edge in _recolor_tier_dot is gated on `sub`/`ids`, so filtering
        # here is sufficient to render exactly those nodes + their internal edges.
        sub = {nid: node for nid, node in sub.items() if nid in only}

    ids = set(sub)
    exp = {pid for pid in (expanded or set()) if pid in ids and has_children(pid, nodes)}

    lines: List[str] = ['strict digraph "" {']
    lines.extend(eb._graph_attr_lines())
    # Massot-style layout override (a later `graph [...]` wins over the shared default
    # appended by eb._graph_attr_lines): top→bottom, compact ranks, concentrate off,
    # and splines sized to the rendered-node count so the leanblueprint look (curved)
    # stays affordable now that transitive reduction has cut the drawn edge set.
    #
    #   * curved   for ≤60 rendered nodes  (the Massot look — readable, fit-legible)
    #   * polyline for 61–120              (routes around nodes; cheaper than curved)
    #   * line     for >120                (safety net — >120 flat tiers hit the picker,
    #                                       so a full graph this size never renders)
    #
    # Rendered-node count = leaves/collapsed parents drawn as single nodes (sub minus
    # expanded) + the tier-(N+1) children drawn inside every expanded box.
    n_render = len(sub) - len(exp)
    for pid in exp:
        n_render += len(child_ids(pid, nodes))
    if n_render <= 60:
        splines = "curved"
    elif n_render <= 120:
        splines = "polyline"
    else:
        splines = "line"
    lines.append(
        f"  graph [rankdir=TB, ranksep=0.5, nodesep=0.3, concentrate=false, "
        f"splines={splines}];")

    return _recolor_tier_dot(
        nodes, sidecar, sub, ids, name_to_slug, lines, tier, expanded or set(),
        sorry_set=sorry_set or set())


# ---------------------------------------------------------------------------
# whole-graph state (the /api/state payload)
# ---------------------------------------------------------------------------

def compute_state(
    nodes: Dict[str, dict],
    sidecar: dict,
    sorry_set: Optional[Set[str]] = None,
) -> dict:
    """The full computed review state: verdicts, taint, coverage, frontier, dial,
    and per-cluster roll-ups. Pure; this is exactly what ``/api/state`` returns.

    ``sorry_set`` (a set of node ids whose Lean is incomplete, default empty) is
    woven through the whole computation:

      * ``colors`` paint a sorry node violet (``"sorry"``) — top precedence over
        every verdict and over in_mathlib;
      * ``tainted`` adds the sorry nodes as taint sources (sources = flagged ∪
        rejected ∪ sorry), so a sorry's forward ``depends_on`` closure is hatched;
      * ``coverage`` / ``trust_frontier`` no longer count a sorry node (or a sink
        resting on one) as trusted;
      * the payload gains a ``"sorry"`` key listing the sorted node ids.

    ``sorry_set=None`` (the default) reproduces the original behavior byte-for-byte:
    an empty set means no sorry sources, no violet colors, and ``"sorry": []``.
    """
    sorry_set = sorry_set or set()
    tainted = sorted(tainted_set(nodes, sidecar, sorry_set))
    clusters = sorted(
        nid for nid, node in nodes.items() if eb.node_tier(node) == 1)
    verdicts = {nid: verdict_of(nid, sidecar) for nid in nodes}
    return {
        "dial": dial_of(sidecar),
        "verdicts": verdicts,
        # color_state per node: the trust-state color the UI paints (violet for a
        # sorry/incomplete node, blue for an in-Mathlib reuse, else the effective
        # verdict). Alongside `verdicts` so the client/tests can distinguish each.
        "colors": {nid: color_state(nodes[nid], verdicts[nid], nid in sorry_set)
                   for nid in nodes},
        "sources": {nid: review_source(nid, sidecar) for nid in nodes},
        "tainted": tainted,
        "sorry": sorted(nid for nid in sorry_set if nid in nodes),
        "coverage": coverage(nodes, sidecar, sorry_set),
        "trust_frontier": trust_frontier(nodes, sidecar, sorry_set),
        "clusters": {cid: cluster_rollup(cid, nodes, sidecar) for cid in clusters},
    }


def apply_human_verdict(
    sidecar: dict,
    node_id: str,
    verdict: str,
    score: Optional[int],
    note: str,
    by: str,
    at: str,
) -> dict:
    """Return a NEW sidecar dict with the human slot for ``node_id`` set.

    Pure: does not write to disk (the server owns the single write). The human slot
    is the only thing this touches — the ``ai`` slot is left intact (human is
    immutable and additive over ai). Raises ValueError on an invalid verdict.
    """
    if verdict not in VERDICTS:
        raise ValueError(f"invalid verdict: {verdict!r} (want one of {VERDICTS})")
    out = json.loads(json.dumps(sidecar))  # deep copy
    out.setdefault("reviews", {})
    rec = out["reviews"].setdefault(node_id, {})
    rec["human"] = {
        "verdict": verdict,
        "score": score,
        "note": note or "",
        "by": by or "reviewer",
        "at": at,
    }
    out["updated_at"] = at
    return out
