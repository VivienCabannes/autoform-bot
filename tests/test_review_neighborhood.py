"""Tests for the bounded-local-view backend (review_model).

Covers ``neighborhood`` (BFS by hop over ``depends_on`` in BOTH directions, anchors
always included, bounded to ``cap`` keeping the closest in stable order) and
``recolor_dot(only=…)`` (render ONLY those nodes + the edges BETWEEN them — a
subgraph — with the same coloring / encodings / expansion rules).
"""
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "scripts" / "review_ui"))
sys.path.insert(0, str(_HERE.parent / "scripts"))

import review_model as rm        # noqa: E402
import export_blueprint as eb    # noqa: E402


def _sc(reviews=None):
    sc = rm.empty_sidecar()
    sc["reviews"] = reviews or {}
    return sc


def _chain(n, tier=3):
    """A tier-`tier` chain d0 -> d1 -> ... (each depends_on the previous)."""
    g = {}
    for i in range(n):
        g[f"d{i}"] = {
            "id": f"d{i}", "tier": tier, "parent": "s1", "kind": "lemma",
            "name": f"ns.d{i}", "mathlib_status": "missing",
            "depends_on": [f"d{i-1}"] if i else [],
        }
    return g


# --- direction: both deps and dependents -----------------------------------

def test_neighborhood_includes_anchor_only_at_radius_zero():
    g = _chain(5)
    nb = rm.neighborhood({"d2"}, g, tier=3, radius=0, cap=60)
    assert nb == {"d2"}


def test_neighborhood_both_directions_one_hop():
    # chain d1 -> d2 -> d3 : a 1-hop nbhd of d2 reaches BOTH its dep (d1, since
    # d2 depends_on d1) and its dependent (d3, since d3 depends_on d2).
    g = _chain(5)
    nb = rm.neighborhood({"d2"}, g, tier=3, radius=1, cap=60)
    assert nb == {"d1", "d2", "d3"}


def test_neighborhood_radius_grows_by_hop():
    g = _chain(7)
    nb1 = rm.neighborhood({"d3"}, g, tier=3, radius=1, cap=60)
    nb2 = rm.neighborhood({"d3"}, g, tier=3, radius=2, cap=60)
    assert nb1 == {"d2", "d3", "d4"}
    assert nb2 == {"d1", "d2", "d3", "d4", "d5"}
    assert nb1 <= nb2


# --- cap: keep the closest, stable order, anchors always kept ---------------

def test_neighborhood_cap_keeps_closest_and_is_bounded():
    g = _chain(40)
    nb = rm.neighborhood({"d20"}, g, tier=3, radius=10, cap=5)
    assert len(nb) <= 5
    assert "d20" in nb                       # anchor always present
    # the closest neighbors survive (d19/d21 are 1 hop) and far nodes do not
    assert "d19" in nb and "d21" in nb
    assert "d0" not in nb and "d39" not in nb


def test_neighborhood_anchor_always_in_even_past_cap():
    g = _chain(10)
    # cap smaller than the seed set: every anchor is still kept.
    nb = rm.neighborhood({"d1", "d4", "d7"}, g, tier=3, radius=3, cap=2)
    assert {"d1", "d4", "d7"} <= nb


def test_neighborhood_unknown_or_offtier_anchor_yields_nothing():
    g = _chain(5)
    assert rm.neighborhood({"nope"}, g, tier=3, radius=2, cap=60) == set()
    # an anchor that exists but is the wrong tier seeds no frontier
    g["s1"] = {"id": "s1", "tier": 2, "parent": None, "depends_on": []}
    assert rm.neighborhood({"s1"}, g, tier=3, radius=2, cap=60) == set()


def test_neighborhood_restricted_to_its_tier():
    # tier-2 anchor only ever reaches tier-2 nodes, never the tier-3 chain.
    g = _chain(4)
    g.update({
        "s1": {"id": "s1", "tier": 2, "parent": "cA", "depends_on": []},
        "s2": {"id": "s2", "tier": 2, "parent": "cA", "depends_on": ["s1"]},
    })
    nb = rm.neighborhood({"s1"}, g, tier=2, radius=1, cap=60)
    assert nb == {"s1", "s2"}
    assert all(eb.node_tier(g[n]) == 2 for n in nb)


# --- recolor_dot(only=…) : subset of nodes, only internal edges -------------

def _diamond():
    # d1 -> d2, d1 -> d3, d2 -> d4, d3 -> d4 (a tier-3 diamond) + an outsider d5.
    return {
        "s1": {"id": "s1", "tier": 2, "parent": "cA", "depends_on": []},
        "d1": {"id": "d1", "tier": 3, "parent": "s1", "kind": "lemma",
               "name": "ns.d1", "mathlib_status": "missing", "depends_on": []},
        "d2": {"id": "d2", "tier": 3, "parent": "s1", "kind": "lemma",
               "name": "ns.d2", "mathlib_status": "missing", "depends_on": ["d1"]},
        "d3": {"id": "d3", "tier": 3, "parent": "s1", "kind": "lemma",
               "name": "ns.d3", "mathlib_status": "missing", "depends_on": ["d1"]},
        "d4": {"id": "d4", "tier": 3, "parent": "s1", "kind": "theorem",
               "name": "ns.d4", "mathlib_status": "missing",
               "depends_on": ["d2", "d3"]},
        "d5": {"id": "d5", "tier": 3, "parent": "s1", "kind": "lemma",
               "name": "ns.d5", "mathlib_status": "missing", "depends_on": ["d4"]},
    }


def test_recolor_dot_only_renders_just_the_subset():
    g = _diamond()
    slug = eb.build_slug_map(g)
    dot = rm.recolor_dot(g, _sc(), tier=3, only={"d1", "d2", "d3"})
    for keep in ("d1", "d2", "d3"):
        assert f'"{slug[keep]}" [' in dot
    for drop in ("d4", "d5"):
        assert f'"{slug[drop]}" [' not in dot


def test_recolor_dot_only_keeps_internal_edges_only():
    g = _diamond()
    slug = eb.build_slug_map(g)
    # only = {d1, d2, d3}: keep d1->d2 and d1->d3; drop every edge to d4 (outside).
    dot = rm.recolor_dot(g, _sc(), tier=3, only={"d1", "d2", "d3"})
    assert f'"{slug["d1"]}" -> "{slug["d2"]}"' in dot
    assert f'"{slug["d1"]}" -> "{slug["d3"]}"' in dot
    # edges touching an out-of-view node never appear
    assert f'"{slug["d2"]}" -> "{slug["d4"]}"' not in dot
    assert f'"{slug["d3"]}" -> "{slug["d4"]}"' not in dot
    assert f'"{slug["d4"]}"' not in dot


def test_recolor_dot_only_preserves_coloring_and_encodings():
    # a rejected node inside the subset still paints its trust color + class.
    g = _diamond()
    slug = eb.build_slug_map(g)
    sc = _sc({"d2": {"ai": {"faithfulness": 2, "proof_integrity": 3,
                            "code_quality": 4, "verdict": "rejected"}}})
    dot = rm.recolor_dot(g, sc, tier=3, only={"d1", "d2", "d3"})
    line = next(ln for ln in dot.splitlines() if f'"{slug["d2"]}" [' in ln)
    assert "rv-rejected" in line
    assert rm.VERDICT_DOT["rejected"]["color"] in line


def test_recolor_dot_only_none_is_full_tier():
    g = _diamond()
    slug = eb.build_slug_map(g)
    full = rm.recolor_dot(g, _sc(), tier=3)            # only=None
    for d in ("d1", "d2", "d3", "d4", "d5"):
        assert f'"{slug[d]}" [' in full


def test_recolor_dot_only_matches_a_neighborhood():
    # the realistic pairing: only = neighborhood({anchor}, …) renders that subgraph.
    g = _diamond()
    slug = eb.build_slug_map(g)
    nb = rm.neighborhood({"d2"}, g, tier=3, radius=1, cap=60)
    assert "d2" in nb
    dot = rm.recolor_dot(g, _sc(), tier=3, only=nb)
    for n in nb:
        assert f'"{slug[n]}" [' in dot
    # a node outside the bounded neighborhood is not drawn
    outside = {"d1", "d2", "d3", "d4", "d5"} - nb
    for o in outside:
        assert f'"{slug[o]}" [' not in dot


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
