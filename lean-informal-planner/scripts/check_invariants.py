#!/usr/bin/env python3
"""Deterministic structural check for a formalization plan's graph.json.

A review wave partitions the graph for responsibility, so each reviewer sees
only its own slice; the global structural invariants that span the whole graph
are nobody's local view. This checker is that global view. The orchestrator runs
it after each review wave to confirm the wave kept the graph well-formed.

It verifies, against skills/plan/references/plan-json-schema.md:

  - reference integrity: every depends_on target and every non-null parent
    resolves to an existing node;
  - tier discipline: depends_on edges stay within one tier, and a parent sits
    exactly one tier above its child;
  - per-tier acyclicity: no cycles among same-tier depends_on edges;
  - root reachability: every "missing" node reaches an "in-mathlib" node by
    following depends_on, and every root (empty depends_on) is "in-mathlib".

Each invariant prints a PASS/FAIL line naming the specific offending ids. The
exit code is 0 only when every invariant passes, 1 when any fails.

Usage:
    check_invariants.py <graph.json>
"""
from __future__ import annotations

import argparse
import json
import os
import sys


def _load_nodes(graph_path: str) -> dict:
    with open(graph_path, encoding="utf-8") as f:
        graph = json.load(f)
    nodes = graph.get("nodes")
    if not isinstance(nodes, dict):
        raise ValueError("graph.json has no 'nodes' map")
    return nodes


def _report(name: str, offenders: list[str], detail: str = "") -> bool:
    """Print one PASS/FAIL line for an invariant; return True when it passes."""
    if not offenders:
        print(f"PASS  {name}")
        return True
    shown = "; ".join(offenders)
    suffix = f" ({detail})" if detail else ""
    print(f"FAIL  {name}{suffix}: {shown}")
    return False


def check_references(nodes: dict) -> bool:
    """Every depends_on target and every non-null parent names an existing node."""
    offenders = []
    for nid, rec in nodes.items():
        parent = rec.get("parent")
        if parent is not None and parent not in nodes:
            offenders.append(f"{nid} -> parent {parent!r} (absent)")
        for dep in rec.get("depends_on") or []:
            if dep not in nodes:
                offenders.append(f"{nid} -> depends_on {dep!r} (absent)")
    return _report("reference integrity", offenders)


def check_tiers(nodes: dict) -> bool:
    """depends_on stays within a tier; parent sits exactly one tier above its child."""
    offenders = []
    for nid, rec in nodes.items():
        tier = rec.get("tier")
        parent = rec.get("parent")
        if parent is not None and parent in nodes:
            ptier = nodes[parent].get("tier")
            if tier is None or ptier is None or ptier != tier - 1:
                offenders.append(f"{nid} (tier {tier}) -> parent {parent!r} (tier {ptier})")
        for dep in rec.get("depends_on") or []:
            if dep in nodes:
                dtier = nodes[dep].get("tier")
                if dtier != tier:
                    offenders.append(f"{nid} (tier {tier}) -> depends_on {dep!r} (tier {dtier})")
    return _report("tier discipline", offenders)


def _find_cycle(nodes: dict, tier) -> list[str] | None:
    """Return one within-tier depends_on cycle as an id list, or None if acyclic."""
    members = {nid for nid, rec in nodes.items() if rec.get("tier") == tier}
    WHITE, GREY, BLACK = 0, 1, 2
    color = {nid: WHITE for nid in members}

    def edges(nid):
        for dep in nodes[nid].get("depends_on") or []:
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
                    return path[path.index(dep):] + [dep]
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


def check_reachability(nodes: dict) -> bool:
    """Every 'missing' node reaches an 'in-mathlib' node; roots are 'in-mathlib'."""
    # A node "grounds" if it is in-mathlib or some depends_on target grounds.
    grounded: dict[str, bool] = {}

    def grounds(nid: str, seen: set[str]) -> bool:
        if nid in grounded:
            return grounded[nid]
        if nid in seen:  # cycle: acyclicity is checked separately; treat as ungrounded here
            return False
        seen.add(nid)
        rec = nodes[nid]
        if rec.get("mathlib_status") == "in-mathlib":
            grounded[nid] = True
            return True
        result = any(
            dep in nodes and grounds(dep, seen)
            for dep in rec.get("depends_on") or []
        )
        grounded[nid] = result
        return result

    unsupported = [
        nid for nid, rec in nodes.items()
        if rec.get("mathlib_status") == "missing" and not grounds(nid, set())
    ]
    bad_roots = [
        nid for nid, rec in nodes.items()
        if not (rec.get("depends_on") or []) and rec.get("mathlib_status") != "in-mathlib"
    ]
    offenders = (
        [f"{nid} (missing, no in-mathlib root)" for nid in unsupported]
        + [f"{nid} (root, status {nodes[nid].get('mathlib_status')!r})" for nid in bad_roots]
    )
    return _report("root reachability", offenders)


def check(graph_path: str) -> bool:
    nodes = _load_nodes(graph_path)
    results = [
        check_references(nodes),
        check_tiers(nodes),
        check_acyclic(nodes),
        check_reachability(nodes),
    ]
    return all(results)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Deterministic structural check of a plan's graph.json"
    )
    ap.add_argument("graph", help="path to graph.json")
    args = ap.parse_args(argv)

    if not os.path.exists(args.graph):
        print(f"error: {args.graph} does not exist", file=sys.stderr)
        return 2

    ok = check(args.graph)
    print("OK: all invariants hold" if ok else "FAILED: one or more invariants violated")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
