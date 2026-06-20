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
Schema (see SHARED_SPEC.md)::

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
# Palette (SHARED_SPEC) — verdict -> (DOT border color, fill color). These are the
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
    "grey": "#C9C2B4",  # unreviewed
}

# Color-state -> DOT colors. Fills are light tints; borders are the palette.
# "in_mathlib" (blue) is a trust state, not a verdict: a node already in Mathlib.
VERDICT_DOT = {
    "in_mathlib": {"color": PALETTE["in_mathlib"], "fill": "#DCE8F7"},
    "clean":      {"color": PALETTE["clean"],    "fill": "#D6EAD9"},
    "flagged":    {"color": PALETTE["flagged"],  "fill": "#F2E4C4"},
    "rejected":   {"color": PALETTE["rejected"], "fill": "#F1D2CE"},
    "unreviewed": {"color": PALETTE["grey"],     "fill": "#ECE7DC"},
}

# mathlib_status values that mean a node is *fully* in Mathlib (=> blue, trusted).
# Canonical value is "exists"; tolerate the other spellings seen in the wild.
IN_MATHLIB_STATUSES = {"exists", "in-mathlib", "in_mathlib", "mathlib"}

# The three jury rubrics: (name, weight, pass_threshold). Mirrors eval-rubrics.
RUBRICS: List[Tuple[str, float, int]] = [
    ("faithfulness", 0.40, 4),
    ("proof_integrity", 0.40, 3),
    ("code_quality", 0.20, 3),
]

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
# jury scoring -> verdict (Feature 6, threshold-gated, NOT the average)
# ---------------------------------------------------------------------------

def weighted_score(scores: dict) -> Optional[float]:
    """Displayed score = 0.40*faith + 0.40*integ + 0.20*qual (0-5), or None.

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
    """Map three rubric scores to a verdict, threshold-gated (Feature 6).

      * rejected — faithfulness <=2 OR proof_integrity <=2 (a correctness rubric
        materially wrong / cheating). Style can never reach here.
      * clean    — all pass (faith >=4, integ >=3, qual >=3).
      * flagged  — otherwise (e.g. faith ==3, or code_quality <=2).

    Missing scores are treated conservatively as a fail (cannot be clean).
    """
    faith = scores.get("faithfulness")
    integ = scores.get("proof_integrity")
    qual = scores.get("code_quality")

    # rejected: a correctness axis materially wrong. Style never rejects.
    if (faith is not None and faith <= 2) or (integ is not None and integ <= 2):
        return "rejected"

    passes = (
        faith is not None and faith >= 4
        and integ is not None and integ >= 3
        and qual is not None and qual >= 3
    )
    if passes:
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


def color_state(node: dict, effective_verdict: str) -> str:
    """The **trust-state color** for a node: one of ``in_mathlib`` (blue) /
    ``clean`` (green) / ``flagged`` (amber) / ``rejected`` (red) / ``unreviewed``
    (grey).

    A real defect (flagged/rejected) shows even on a reuse — e.g. a wrong Mathlib
    lemma cited — so it overrides the blue. Otherwise an in-Mathlib node is blue;
    everything else keeps its effective verdict (clean -> green, else grey).
    """
    if effective_verdict in ("rejected", "flagged"):
        return effective_verdict
    if is_in_mathlib(node):
        return "in_mathlib"
    return effective_verdict


def is_trusted(node_id: str, node: dict, sidecar: dict) -> bool:
    """Whether a node is *trusted* for taint/frontier/coverage.

    Trusted = effective verdict ``clean`` (ours, reviewed clean) **OR** the node
    is in Mathlib (trusted by construction) and carries no defect verdict. A
    flagged/rejected node is never trusted, even if it is in Mathlib.
    """
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
            "faithfulness": ai.get("faithfulness"),
            "proof_integrity": ai.get("proof_integrity"),
            "code_quality": ai.get("code_quality"),
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


def tainted_set(nodes: Dict[str, dict], sidecar: dict) -> Set[str]:
    """The set of node ids tainted by some flagged/rejected ancestor.

    A node is *tainted* if any node in its ``depends_on`` transitive closure has an
    effective verdict of flagged or rejected. Computed live, never stored. The bad
    nodes themselves are NOT in the tainted set (they are the source, not victims) —
    they carry their own verdict color; taint marks the *downstream* trust damage.

    Only flagged/rejected nodes taint: an ``in_mathlib`` (blue) node is trusted by
    construction and never seeds taint (it has no flagged/rejected verdict).
    """
    rev = _dependents_index(nodes)
    bad = [nid for nid in nodes
           if verdict_of(nid, sidecar) in ("flagged", "rejected")]

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
# tier-1 cluster roll-up
# ---------------------------------------------------------------------------

def _tier2_children(cluster_id: str, nodes: Dict[str, dict]) -> List[str]:
    """The tier-2 node ids whose parent is this tier-1 cluster, sorted."""
    return sorted(
        nid for nid, node in nodes.items()
        if eb.node_tier(node) == 2 and node.get("parent") == cluster_id
    )


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


# ---------------------------------------------------------------------------
# coverage + trust frontier
# ---------------------------------------------------------------------------

def coverage(nodes: Dict[str, dict], sidecar: dict) -> dict:
    """Review coverage over tier-2 nodes: how many have any effective verdict.

    Coverage is the header progress bar: reviewed / total tier-2. "Reviewed" means
    a node has an effective verdict (ai or human), i.e. it is not ``unreviewed``.
    """
    tier2 = [nid for nid, node in nodes.items() if eb.node_tier(node) == 2]
    reviewed = [nid for nid in tier2 if verdict_of(nid, sidecar) != "unreviewed"]
    human = [nid for nid in tier2 if review_source(nid, sidecar) == "human"]
    in_mathlib = [nid for nid in tier2 if is_in_mathlib(nodes[nid])]
    trusted = [nid for nid in tier2 if is_trusted(nid, nodes[nid], sidecar)]
    total = len(tier2)
    return {
        "total": total,
        "reviewed": len(reviewed),
        "human_confirmed": len(human),
        "in_mathlib": len(in_mathlib),
        "trusted": len(trusted),
        "fraction": (len(reviewed) / total) if total else 0.0,
    }


def trust_frontier(nodes: Dict[str, dict], sidecar: dict) -> List[str]:
    """The sink nodes (top-level results) resting on a fully-**trusted** closure.

    A sink is a tier-2 node that nothing else (in tier 2) depends on. A sink is on
    the trust frontier when it AND its entire ``depends_on`` closure are all
    *trusted* — each node is either reviewed clean or already in Mathlib (with no
    flagged/rejected defect). A blue (in-Mathlib) node therefore counts toward the
    frontier exactly like a clean one. These are the results a human can currently
    trust end-to-end. Sorted for determinism.
    """
    tier2 = {nid: node for nid, node in nodes.items() if eb.node_tier(node) == 2}
    rev = _dependents_index(tier2)
    sinks = [nid for nid in tier2 if not rev.get(nid)]

    frontier: List[str] = []
    for sink in sinks:
        closure = _closure(sink, tier2)
        if all(is_trusted(nid, tier2[nid], sidecar) for nid in closure):
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
) -> str:
    """Emit one DOT node line colored by the **trust state** (color_state), not the
    raw verdict.

    Encodings:
      * fill/border color = color_state (in_mathlib=blue / clean=green /
        flagged=amber / rejected=red / unreviewed=grey);
      * in_mathlib node    => **solid** (trusted by construction — no AI-only ring,
        never hatched), even when no human has reviewed it;
      * AI-only node       => dashed border ring (style=dashed);
      * human-confirmed    => solid filled;
      * tainted node       => 45-degree hatch overlay (style includes "diagonals"
        + a striped look approximated with style=striped fill, plus a data attr the
        client CSS uses for an exact 45deg hatch on the SVG).
    Shape still follows ``kind`` (box for definitions) exactly as the exporter does,
    so layout is identical to the exported graph.
    """
    kind = (node.get("kind") or "theorem").lower()
    shape = "box" if kind in eb.DEFINITION_KINDS else "ellipse"

    state = color_state(node, verdict)
    blue = state == "in_mathlib"

    colors = VERDICT_DOT.get(state, VERDICT_DOT["unreviewed"])
    attrs: List[str] = [
        f'label="{eb._dot_escape(nid)}"',
        f'color="{colors["color"]}"',
        f'fillcolor="{colors["fill"]}"',
        f"shape={shape}",
    ]

    # style: filled always. A blue (in-Mathlib) node is trusted by construction:
    # solid, no AI-only ring and never hatched. Otherwise: dashed = AI-only ring,
    # "diagonals" hints taint.
    styles = ["filled"]
    if not blue and source != "human":
        # AI-only (or unreviewed) -> dashed ring marks "provisional / unvouched".
        styles.append("dashed")
    if tainted and not blue:
        # graphviz "diagonals" decorates the node; the client overlays a true 45deg
        # hatch via the class we tag below, so this is a graceful fallback.
        styles.append("diagonals")
    attrs.append(f'style="{",".join(styles)}"')

    # A class the client SVG post-processor keys on for the exact hatch + ring.
    # Blue nodes are solid + never tainted/AI-only-styled (trusted by construction).
    if blue:
        klass = "rv-in_mathlib rv-solid"
    else:
        klass = f"rv-{state}" + (" rv-tainted" if tainted else "") + (
            " rv-aionly" if source != "human" else " rv-human")
    attrs.append(f'class="{klass}"')

    return f'  "{slug}" [{", ".join(attrs)}];'


def recolor_dot(
    nodes: Dict[str, dict],
    sidecar: dict,
    tier: int = 2,
) -> str:
    """Build a DOT digraph for the given tier, recolored by effective verdict.

    Reuses the exporter's ``_graph_attr_lines`` (shared graph/node/edge attrs) and
    slug map, so the recolored graph is laid out identically to the exported one —
    only the node colors/styles change (verdict instead of mathlib_status) plus the
    dashed-ring / hatch encodings. Within-tier ``depends_on`` edges are emitted
    dashed, matching ``export_blueprint.build_tier2_dot``.
    """
    name_to_slug = eb.build_slug_map(nodes)
    sub = {nid: node for nid, node in nodes.items() if eb.node_tier(node) == tier}
    tainted = tainted_set(nodes, sidecar)

    lines: List[str] = ['strict digraph "" {']
    lines.extend(eb._graph_attr_lines())

    ids = set(sub)
    for nid in sorted(sub):
        lines.append(_verdict_node_dot(
            nid, sub[nid], name_to_slug[nid],
            verdict_of(nid, sidecar),
            review_source(nid, sidecar),
            nid in tainted,
        ))

    edges: List[Tuple[str, str]] = []
    for nid, node in sub.items():
        for dep in node.get("depends_on", []) or []:
            if dep in ids and dep != nid:
                edges.append((name_to_slug[dep], name_to_slug[nid]))
    for s, t in sorted(set(edges)):
        lines.append(f'  "{s}" -> "{t}" [style=dashed];')

    lines.append("}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# whole-graph state (the /api/state payload)
# ---------------------------------------------------------------------------

def compute_state(nodes: Dict[str, dict], sidecar: dict) -> dict:
    """The full computed review state: verdicts, taint, coverage, frontier, dial,
    and per-cluster roll-ups. Pure; this is exactly what ``/api/state`` returns."""
    tainted = sorted(tainted_set(nodes, sidecar))
    clusters = sorted(
        nid for nid, node in nodes.items() if eb.node_tier(node) == 1)
    verdicts = {nid: verdict_of(nid, sidecar) for nid in nodes}
    return {
        "dial": dial_of(sidecar),
        "verdicts": verdicts,
        # color_state per node: the trust-state color the UI paints (blue for an
        # in-Mathlib reuse, else the effective verdict). Alongside `verdicts` so the
        # client/tests can distinguish blue from green/grey.
        "colors": {nid: color_state(nodes[nid], verdicts[nid]) for nid in nodes},
        "sources": {nid: review_source(nid, sidecar) for nid in nodes},
        "tainted": tainted,
        "coverage": coverage(nodes, sidecar),
        "trust_frontier": trust_frontier(nodes, sidecar),
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
