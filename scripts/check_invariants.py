#!/usr/bin/env python3
"""Deterministic structural check for a formalization plan's graph.json.

A review wave partitions the graph by responsibility; this checker verifies the
global structural invariants that span the whole graph. The orchestrator runs it
after each review wave to confirm the graph stayed well-formed.

It separates two kinds of check, against internal/references/plan-json-schema.md:

  Structural integrity (must hold at every stage):
  - reference integrity: every dependency/related target and non-null parent
    resolves to an existing node;
  - edge consistency: depends_on is the union of the typed dependency fields;
  - tier discipline: dependency edges stay within one tier, and a parent sits
    exactly one tier above its child;
  - per-tier acyclicity: no cycles among same-tier depends_on edges.

  Grounding completeness (the goal a phase reaches, not an every-wave gate):
  - every "missing" node reaches an "in-mathlib" node by following depends_on,
    and every root (empty depends_on) is "in-mathlib".

Run it after each review wave for structural integrity; mid-build, still-ungrounded
nodes are simply the remaining work, so grounding does not affect the exit code
unless --require-grounding is given (use that at the end of a phase). Each check
prints a PASS/FAIL line naming the offending ids. Exit code: 0 when the gating
checks pass, 1 on a violation, 2 if the path is missing.

Usage:
    check_invariants.py <graph.json> [--require-grounding]
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys

try:
    from .graph_contract import HARD_EDGE_KINDS, SCHEMA_VERSION, normalize_edges, project_edges
except ImportError:  # direct script execution
    from graph_contract import HARD_EDGE_KINDS, SCHEMA_VERSION, normalize_edges, project_edges


def _load_graph(graph_path: str) -> dict:
    with open(graph_path, encoding="utf-8") as f:
        graph = json.load(f)
    nodes = graph.get("nodes")
    if not isinstance(nodes, dict):
        raise ValueError("graph.json has no 'nodes' map")
    return graph


def _report(name: str, offenders: list[str], detail: str = "") -> bool:
    """Print one PASS/FAIL line for an invariant; return True when it passes."""
    if not offenders:
        print(f"PASS  {name}")
        return True
    shown = "; ".join(offenders)
    suffix = f" ({detail})" if detail else ""
    print(f"FAIL  {name}{suffix}: {shown}")
    return False


_TYPED_DEPENDENCIES = ("statement_depends_on", "proof_depends_on")


def _dependencies(record: dict) -> list[str]:
    return record.get("depends_on") or []


def check_references(nodes: dict, edges: list[dict] | None = None) -> bool:
    """Every structural reference names an existing node."""
    offenders = []
    for nid, rec in nodes.items():
        parent = rec.get("parent")
        if parent is not None and parent not in nodes:
            offenders.append(f"{nid} -> parent {parent!r} (absent)")
        for dep in _dependencies(rec):
            if dep not in nodes:
                offenders.append(f"{nid} -> depends_on {dep!r} (absent)")
        for related in rec.get("related") or []:
            if related not in nodes:
                offenders.append(f"{nid} -> related {related!r} (absent)")
    for edge in edges or []:
        for endpoint in ("source", "target"):
            if edge.get(endpoint) not in nodes:
                offenders.append(f"{edge.get('id', '<edge>')} -> {endpoint} {edge.get(endpoint)!r} (absent)")
    return _report("reference integrity", offenders)


def check_edge_consistency(nodes: dict, edges: list[dict] | None = None, *, canonical: bool = False) -> bool:
    """Typed dependencies are well-formed and agree with the scheduler union."""
    offenders = []
    expected_nodes = None
    if canonical:
        expected_graph = {"version": SCHEMA_VERSION, "nodes": copy.deepcopy(nodes), "edges": copy.deepcopy(edges or [])}
        project_edges(expected_graph, expected_graph["edges"])
        expected_nodes = expected_graph["nodes"]
    for nid, rec in nodes.items():
        legacy = rec.get("depends_on") or []
        if not isinstance(legacy, list) or not all(isinstance(dep, str) for dep in legacy):
            offenders.append(f"{nid}: depends_on is not a string list")
            continue
        if not any(field in rec for field in _TYPED_DEPENDENCIES):
            continue
        union: list[str] = []
        valid = True
        for field in _TYPED_DEPENDENCIES:
            values = rec.get(field) or []
            if not isinstance(values, list) or not all(isinstance(dep, str) for dep in values):
                offenders.append(f"{nid}: {field} is not a string list")
                valid = False
            else:
                union.extend(values)
        if valid and list(dict.fromkeys(union)) != legacy:
            offenders.append(f"{nid}: depends_on does not equal typed dependency union")
        if expected_nodes is not None:
            for field in (*_TYPED_DEPENDENCIES, "depends_on", "related"):
                if rec.get(field, []) != expected_nodes[nid].get(field, []):
                    offenders.append(f"{nid}: {field} does not match canonical edges")
            aliases = rec.get("aliases", [])
            if not isinstance(aliases, list) or not all(isinstance(alias, str) and alias for alias in aliases):
                offenders.append(f"{nid}: aliases is not a non-empty string list")
    return _report("typed edge consistency", offenders)


def check_tiers(nodes: dict, edges: list[dict] | None = None) -> bool:
    """depends_on stays within a tier; parent sits exactly one tier above its child."""
    offenders = []
    for nid, rec in nodes.items():
        tier = rec.get("tier")
        parent = rec.get("parent")
        if parent is not None and parent in nodes:
            ptier = nodes[parent].get("tier")
            if tier is None or ptier is None or ptier != tier - 1:
                offenders.append(f"{nid} (tier {tier}) -> parent {parent!r} (tier {ptier})")
        for dep in _dependencies(rec):
            if dep in nodes:
                dtier = nodes[dep].get("tier")
                if dtier != tier:
                    offenders.append(f"{nid} (tier {tier}) -> depends_on {dep!r} (tier {dtier})")
    if edges is not None:
        for edge in edges:
            if edge.get("kind") not in HARD_EDGE_KINDS:
                continue
            source, target = edge["source"], edge["target"]
            if source in nodes and target in nodes and nodes[source].get("tier") != nodes[target].get("tier"):
                offenders.append(
                    f"{source} (tier {nodes[source].get('tier')}) -> {edge['kind']} "
                    f"{target!r} (tier {nodes[target].get('tier')})"
                )
    return _report("tier discipline", offenders)


def _find_cycle(nodes: dict, tier) -> list[str] | None:
    """Return one within-tier depends_on cycle as an id list, or None if acyclic."""
    members = {nid for nid, rec in nodes.items() if rec.get("tier") == tier}
    WHITE, GREY, BLACK = 0, 1, 2
    color = {nid: WHITE for nid in members}

    def edges(nid):
        for dep in _dependencies(nodes[nid]):
            if dep in members:
                yield dep

    for start in members:
        if color[start] != WHITE:
            continue
        # Iterative DFS carrying the current path so a back-edge yields the cycle.
        stack = [(start, iter(edges(start)))]
        path = [start]
        color[start] = GREY
        while stack:
            nid, it = stack[-1]
            advanced = False
            for dep in it:
                if color[dep] == GREY:
                    return path[path.index(dep) :] + [dep]
                if color[dep] == WHITE:
                    color[dep] = GREY
                    path.append(dep)
                    stack.append((dep, iter(edges(dep))))
                    advanced = True
                    break
            if not advanced:
                color[nid] = BLACK
                path.pop()
                stack.pop()
    return None


def check_acyclic(nodes: dict) -> bool:
    """No cycles among same-tier depends_on edges, checked per tier."""
    offenders = []
    for tier in sorted({rec.get("tier") for rec in nodes.values()}, key=lambda t: (t is None, t)):
        cycle = _find_cycle(nodes, tier)
        if cycle is not None:
            offenders.append(f"tier {tier}: " + " -> ".join(cycle))
    return _report("per-tier acyclicity", offenders)


# mathlib_status vocabulary — MIRROR of review_model.STATUS_ALIASES, kept
# dependency-free because this script must run stdlib-only in CI. A contract
# test (tests/test_roadmap_audit.py) asserts the two tables stay identical;
# edit both together.
_STATUS_ALIASES = {
    "in-mathlib": "in-mathlib",
    "exists": "in-mathlib",
    "in_mathlib": "in-mathlib",
    "mathlib": "in-mathlib",
    "partial": "partial",
    "partially": "partial",
    "partial-in-mathlib": "partial",
    "missing": "missing",
    "absent": "missing",
    "not-in-mathlib": "missing",
}


def _normalize_status(raw) -> str | None:
    if not isinstance(raw, str):
        return None
    return _STATUS_ALIASES.get(raw.strip().lower())


def check_reachability(nodes: dict) -> bool:
    """Every 'missing' node reaches an 'in-mathlib' node; roots are 'in-mathlib'.

    Statuses are NORMALIZED first — a graph written with the dashboard's
    tolerated ``"exists"`` spelling must audit exactly the way it renders.
    """
    # A node "grounds" if it is in-mathlib or some depends_on target grounds.
    grounded: dict[str, bool] = {}

    def grounds(nid: str, seen: set[str]) -> bool:
        if nid in grounded:
            return grounded[nid]
        if nid in seen:  # cycle: acyclicity is checked separately; treat as ungrounded here
            return False
        seen.add(nid)
        rec = nodes[nid]
        if _normalize_status(rec.get("mathlib_status")) == "in-mathlib":
            grounded[nid] = True
            return True
        result = any(dep in nodes and grounds(dep, seen) for dep in _dependencies(rec))
        grounded[nid] = result
        return result

    unsupported = [
        nid
        for nid, rec in nodes.items()
        if _normalize_status(rec.get("mathlib_status")) == "missing" and not grounds(nid, set())
    ]
    bad_roots = [
        nid
        for nid, rec in nodes.items()
        if not _dependencies(rec) and _normalize_status(rec.get("mathlib_status")) != "in-mathlib"
    ]
    offenders = [f"{nid} (missing, no in-mathlib root)" for nid in unsupported] + [
        f"{nid} (root, status {nodes[nid].get('mathlib_status')!r})" for nid in bad_roots
    ]
    return _report("root reachability", offenders)


def check(graph_path: str) -> tuple[bool, bool]:
    """Run all checks; return (structural_ok, grounded_ok). The caller decides
    which of the two gates the exit code."""
    graph = _load_graph(graph_path)
    nodes = graph["nodes"]
    canonical = graph.get("version", 0) >= SCHEMA_VERSION or "edges" in graph
    edges = normalize_edges(graph, migrate=not canonical)
    print("Structural integrity (must hold at every stage):")
    structural = all(
        [
            check_references(nodes, edges),
            check_edge_consistency(nodes, edges, canonical=canonical),
            check_tiers(nodes, edges),
            check_acyclic(nodes),
        ]
    )
    print("\nGrounding completeness (required when a phase finishes):")
    grounded = check_reachability(nodes)
    return structural, grounded


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Deterministic structural check of a plan's graph.json")
    ap.add_argument("graph", help="path to graph.json")
    ap.add_argument(
        "--require-grounding",
        action="store_true",
        help="also fail if any 'missing' node is not yet grounded to an in-mathlib "
        "root (use at the end of a phase; omit during intermediate review waves)",
    )
    args = ap.parse_args(argv)

    if not os.path.exists(args.graph):
        print(f"error: {args.graph} does not exist", file=sys.stderr)
        return 2

    try:
        structural, grounded = check(args.graph)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        print(f"error: invalid graph: {error}", file=sys.stderr)
        return 2
    if not structural:
        print("\nFAILED: structural integrity violated — fix or roll back before continuing.")
        return 1
    if not grounded and args.require_grounding:
        print(
            "\nFAILED: grounding incomplete — every 'missing' node must reach an in-mathlib root before the phase ends."
        )
        return 1
    if not grounded:
        print(
            "\nOK: structurally well-formed. Grounding still incomplete — the nodes "
            "listed above are the remaining grounding work."
        )
        return 0
    print("\nOK: structurally well-formed and fully grounded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
