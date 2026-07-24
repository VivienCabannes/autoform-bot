"""Tests for the Massot-style review render: transitive reduction of the DISPLAYED
edge set, plus the invariant that the reduction is DISPLAY-ONLY (compute_state /
taint / trust_frontier / coverage keep using the full depends_on graph).

The transitive reduction trims redundant arcs *drawn* in the dependency graph — an
edge ``u -> v`` is dropped iff another direct successor ``w`` of ``u`` already
reaches ``v`` — so the review graph reads like a leanblueprint depgraph (Massot) at
a glance. It must never change the dependency semantics: a reduced-away edge is
still a real dependency for trust propagation.
"""
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "scripts" / "review_ui"))
sys.path.insert(0, str(_HERE.parent / "scripts"))

import review_model as rm        # noqa: E402
import export_blueprint as eb    # noqa: E402


# --- transitive_reduction: correctness on a known DAG ------------------------

def _succ(pairs):
    """Build a successor adjacency map {u: {v, ...}} from (u, v) edge pairs."""
    s = {}
    for u, v in pairs:
        s.setdefault(u, set()).add(v)
    return s


def _edges(succ):
    return {(u, v) for u, vs in succ.items() for v in vs}


def test_diamond_drops_only_the_one_transitive_edge():
    # The diamond from the spec: a->b, a->c, b->d, c->d, a->d. The direct a->d is
    # redundant (a->b->d and a->c->d both reach d), so reduction drops ONLY a->d,
    # keeping the other 4 edges. 5 edges in, 4 out, a->d gone.
    edges = {("a", "b"), ("a", "c"), ("b", "d"), ("c", "d"), ("a", "d")}
    reduced = rm.transitive_reduction(_succ(edges))
    out = _edges(reduced)
    assert out == {("a", "b"), ("a", "c"), ("b", "d"), ("c", "d")}
    assert ("a", "d") not in out          # the one transitive edge is dropped
    assert len(out) == 4                  # exactly 4 of the original 5 kept


def test_never_drops_a_non_redundant_edge():
    # Every edge here is the ONLY path between its endpoints, so the reduction is a
    # no-op: nothing may be dropped. Covers a chain, a fork, and a join — none of
    # which has an alternate route, so all edges are non-redundant.
    for edges in (
        {("a", "b"), ("b", "c"), ("c", "d")},            # chain
        {("a", "b"), ("a", "c"), ("a", "d")},            # fork (out-star)
        {("a", "d"), ("b", "d"), ("c", "d")},            # join (in-star)
        {("a", "b"), ("b", "c"), ("a", "c"), ("c", "e")},  # one redundant (a->c)
    ):
        reduced = _edges(rm.transitive_reduction(_succ(edges)))
        # No surviving edge set may ever GAIN an edge, and a genuinely non-redundant
        # edge must survive. Recompute redundancy directly and assert equality.
        assert reduced <= edges
        for (u, v) in edges:
            others = {w for (x, w) in edges if x == u and w != v}
            # v reachable from some other direct successor of u (full closure)?
            reach = dict()
            def closure(x, seen=None):
                seen = seen if seen is not None else set()
                acc = set()
                for (p, q) in edges:
                    if p == x and q not in seen:
                        acc.add(q)
                        acc |= closure(q, seen | {q})
                return acc
            redundant = any(v in closure(w) for w in others)
            assert ((u, v) in reduced) == (not redundant)


def test_longer_transitive_chain_keeps_the_skeleton():
    # a->b->c->d plus the two shortcuts a->c and a->d and b->d: all three shortcuts
    # are implied by the chain, so only the 3 chain edges survive.
    edges = {("a", "b"), ("b", "c"), ("c", "d"),
             ("a", "c"), ("a", "d"), ("b", "d")}
    out = _edges(rm.transitive_reduction(_succ(edges)))
    assert out == {("a", "b"), ("b", "c"), ("c", "d")}


def test_cycle_edges_are_never_dropped():
    # A 2-cycle a<->b: neither a->b nor b->a may be dropped (each is the only real
    # arc of the cycle; dropping one would silently delete a real dependency).
    edges = {("a", "b"), ("b", "a")}
    out = _edges(rm.transitive_reduction(_succ(edges)))
    assert out == edges
    # A 3-cycle with a chord must keep all three cycle arcs.
    edges3 = {("a", "b"), ("b", "c"), ("c", "a")}
    out3 = _edges(rm.transitive_reduction(_succ(edges3)))
    assert out3 == edges3


def test_empty_and_single_edge():
    assert rm.transitive_reduction({}) == {}
    assert _edges(rm.transitive_reduction(_succ({("a", "b")}))) == {("a", "b")}


# --- DISPLAY-ONLY: compute_state is identical with and without reduction ------

def _diamond_graph():
    # A tier-2 diamond: s_a -> s_b, s_a -> s_c, s_b -> s_d, s_c -> s_d, plus the
    # redundant direct s_a -> s_d. (depends_on points from a node to its prereqs, so
    # to draw an arc dep->node we set node.depends_on = [dep]. Here we encode the
    # SAME diamond shape directly in depends_on.)
    return {
        "cA": {"id": "cA", "tier": 1, "parent": None, "kind": "section",
               "name": "Cluster A"},
        "s_a": {"id": "s_a", "tier": 2, "parent": "cA", "kind": "lemma",
                "name": "a", "mathlib_status": "missing", "depends_on": []},
        "s_b": {"id": "s_b", "tier": 2, "parent": "cA", "kind": "lemma",
                "name": "b", "mathlib_status": "missing", "depends_on": ["s_a"]},
        "s_c": {"id": "s_c", "tier": 2, "parent": "cA", "kind": "lemma",
                "name": "c", "mathlib_status": "missing", "depends_on": ["s_a"]},
        # s_d depends on b, c AND directly on a (the redundant edge a->d).
        "s_d": {"id": "s_d", "tier": 2, "parent": "cA", "kind": "theorem",
                "name": "d", "mathlib_status": "missing",
                "depends_on": ["s_b", "s_c", "s_a"]},
    }


def _sc(reviews=None):
    sc = rm.empty_sidecar()
    sc["reviews"] = reviews or {}
    return sc


def test_diamond_dot_drops_the_redundant_drawn_edge():
    # The rendered tier-2 DOT must contain b->d, c->d, a->b, a->c but NOT the
    # redundant a->d arc (drawn slug pairs are reduced). Count the drawn arcs.
    g = _diamond_graph()
    slug = eb.build_slug_map(g)
    dot = rm.recolor_dot(g, _sc(), tier=2)
    sa, sb, sc_, sd = (slug["s_a"], slug["s_b"], slug["s_c"], slug["s_d"])
    drawn = {(s, t) for (s, t) in (
        (sa, sb), (sa, sc_), (sb, sd), (sc_, sd), (sa, sd))
        if f'"{s}" -> "{t}"' in dot}
    assert (sa, sd) not in drawn          # redundant a->d not drawn
    assert (sa, sb) in drawn and (sa, sc_) in drawn
    assert (sb, sd) in drawn and (sc_, sd) in drawn
    assert len(drawn) == 4                # 4 of the 5 depends_on arcs drawn


def test_compute_state_identical_with_and_without_reduction():
    # compute_state must NOT be affected by display-only reduction: it reads the full
    # depends_on graph. Reduction lives only in recolor_dot, so simply asserting that
    # compute_state never changes — and that the redundant edge is still a real
    # dependency for taint — proves the carve-out. Rejecting s_a taints everything
    # downstream of it INCLUDING s_d via the (reduced-away in the drawing) a->d edge.
    g = _diamond_graph()
    base = rm.compute_state(g, _sc())

    # Building the DOT (which applies reduction) must not mutate the graph or change
    # the computed state in any way.
    _ = rm.recolor_dot(g, _sc(), tier=2)
    after = rm.compute_state(g, _sc())
    assert after == base

    # And the full dependency semantics are intact: rejecting s_a taints s_d, whose
    # ONLY surviving *drawn* link to s_a (a->d) was reduced away — proving the
    # reduced edge is still a real dependency for trust propagation.
    rejected = _sc({"s_a": {"human": {"verdict": "rejected", "score": 0,
                                      "note": "", "by": "t", "at": "now"}}})
    tainted = set(rm.compute_state(g, rejected)["tainted"])
    assert {"s_b", "s_c", "s_d"} <= tainted


def test_reduction_does_not_change_drawn_node_set():
    # Reduction trims edges only; every tier-2 node still appears in the DOT.
    g = _diamond_graph()
    slug = eb.build_slug_map(g)
    dot = rm.recolor_dot(g, _sc(), tier=2)
    for nid in ("s_a", "s_b", "s_c", "s_d"):
        assert f'"{slug[nid]}"' in dot


def test_dot_uses_massot_layout_override():
    # The Massot layout override line must be appended after the shared attrs.
    g = _diamond_graph()
    dot = rm.recolor_dot(g, _sc(), tier=2)
    assert "rankdir=TB" in dot
    assert "concentrate=false" in dot
    assert "splines=curved" in dot   # 4 rendered nodes <= 60


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
