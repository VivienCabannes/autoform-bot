"""Tests for the N-tier generalization of the review surface.

The old surface only unrolled a tier-1 cluster into its tier-2 children. These tests
exercise the generalization to *any* tier: a tier-2 statement unrolls into its tier-3
declarations the same way a tier-1 cluster unrolls into its tier-2 statements.

Covers ``child_ids`` / ``tiers_present`` / ``has_children``, ``rollup_color`` (with
the ``cluster_color`` alias), and ``recolor_dot(tier=2, expanded=…)`` emitting a
``subgraph cluster_<slug>`` of tier-3 nodes with intra/cross edges.
"""
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "scripts" / "review_ui"))
sys.path.insert(0, str(_HERE.parent / "scripts"))

import review_model as rm        # noqa: E402
import export_blueprint as eb    # noqa: E402


def _graph():
    # 1 cluster (cA) -> 2 statements (s1, s2) -> tier-3 decls.
    #   s1 has decls d1 (ours), d2 (ours, depends on d1), d3 (in mathlib reuse).
    #   s2 has decls e1 (ours). s2 depends_on s1 (a tier-2 -> tier-2 dep).
    return {
        "cA": {"id": "cA", "tier": 1, "parent": None, "kind": "section",
               "name": "Cluster A"},
        "s1": {"id": "s1", "tier": 2, "parent": "cA", "kind": "lemma",
               "name": "Statement one", "mathlib_status": "missing",
               "depends_on": []},
        "s2": {"id": "s2", "tier": 2, "parent": "cA", "kind": "theorem",
               "name": "Statement two", "mathlib_status": "missing",
               "depends_on": ["s1"]},
        "d1": {"id": "d1", "tier": 3, "parent": "s1", "kind": "lemma",
               "name": "ns.d1", "mathlib_status": "missing", "depends_on": []},
        "d2": {"id": "d2", "tier": 3, "parent": "s1", "kind": "theorem",
               "name": "ns.d2", "mathlib_status": "missing",
               "depends_on": ["d1", "d3"]},
        "d3": {"id": "d3", "tier": 3, "parent": "s1", "kind": "lemma",
               "name": "Finset.card_le_card", "mathlib_status": "exists",
               "depends_on": []},
        "e1": {"id": "e1", "tier": 3, "parent": "s2", "kind": "lemma",
               "name": "ns.e1", "mathlib_status": "missing",
               "depends_on": ["d2"]},
    }


def _sc(reviews=None):
    sc = rm.empty_sidecar()
    sc["reviews"] = reviews or {}
    return sc


def _slug():
    return eb.build_slug_map(_graph())


# --- topology helpers -------------------------------------------------------

def test_child_ids_one_tier_down():
    g = _graph()
    assert rm.child_ids("cA", g) == ["s1", "s2"]       # tier-1 -> tier-2
    assert rm.child_ids("s1", g) == ["d1", "d2", "d3"]  # tier-2 -> tier-3
    assert rm.child_ids("s2", g) == ["e1"]
    assert rm.child_ids("d1", g) == []                 # tier-3 leaf
    assert rm.child_ids("nope", g) == []               # unknown parent


def test_has_children():
    g = _graph()
    assert rm.has_children("cA", g)
    assert rm.has_children("s1", g)
    assert not rm.has_children("d1", g)   # leaf
    assert not rm.has_children("d3", g)


def test_tiers_present():
    assert rm.tiers_present(_graph()) == [1, 2, 3]
    # a graph with only tier-2 nodes
    g2 = {"x": {"id": "x", "tier": 2, "parent": None}}
    assert rm.tiers_present(g2) == [2]


# --- rollup_color over direct children (any tier) ---------------------------

def test_rollup_color_tier2_over_tier3_children():
    g = _graph()
    # s1's children: d1 (ours, unreviewed), d2 (ours, unreviewed), d3 (blue).
    # not all blue, not all trusted -> unreviewed.
    assert rm.rollup_color("s1", g, _sc()) == "unreviewed"


def test_rollup_color_flagged_when_child_rejected():
    g = _graph()
    sc = _sc({"d2": {"ai": {"faithfulness": 2, "proof_integrity": 3,
                            "code_quality": 4, "verdict": "rejected"}}})
    assert rm.rollup_color("s1", g, sc) == "flagged"


def test_rollup_color_clean_when_all_children_trusted():
    g = _graph()
    sc = _sc({
        "d1": {"human": {"verdict": "clean"}},
        "d2": {"human": {"verdict": "clean"}},
        # d3 is in mathlib (blue) -> trusted by construction
    })
    assert rm.rollup_color("s1", g, sc) == "clean"


def test_rollup_color_all_in_mathlib_is_blue():
    g = {
        "p": {"id": "p", "tier": 2, "parent": None},
        "c1": {"id": "c1", "tier": 3, "parent": "p", "mathlib_status": "exists"},
        "c2": {"id": "c2", "tier": 3, "parent": "p", "mathlib_status": "exists"},
    }
    assert rm.rollup_color("p", g, _sc()) == "in_mathlib"


def test_cluster_color_is_alias_of_rollup_color():
    g = _graph()
    sc = _sc()
    assert rm.cluster_color("cA", g, sc) == rm.rollup_color("cA", g, sc)


# --- tier-2 expand into tier-3 subgraph -------------------------------------

def test_tier2_collapsed_statements_are_rolled_up_nodes():
    g, slug = _graph(), _slug()
    dot = rm.recolor_dot(g, _sc(), tier=2)  # no expansion
    assert "subgraph cluster_" not in dot
    # both statements are single nodes; their tier-3 decls are NOT drawn.
    assert f'"{slug["s1"]}" [' in dot
    assert f'"{slug["s2"]}" [' in dot
    assert f'"{slug["d1"]}" [' not in dot
    assert f'"{slug["e1"]}" [' not in dot
    # the tier-2 -> tier-2 dependency s1 -> s2 is drawn
    assert f'"{slug["s1"]}" -> "{slug["s2"]}"' in dot


def test_tier2_expand_emits_tier3_subgraph_with_intra_edges():
    g, slug = _graph(), _slug()
    dot = rm.recolor_dot(g, _sc(), tier=2, expanded={"s1"})
    # the box, labelled by the statement name
    assert f"subgraph cluster_{slug['s1']} {{" in dot
    assert 'label="Statement one";' in dot
    # s1's tier-3 children are drawn inside; s1 itself is no longer a single node
    assert f'"{slug["d1"]}" [' in dot
    assert f'"{slug["d2"]}" [' in dot
    assert f'"{slug["d3"]}" [' in dot
    assert f'"{slug["s1"]}" [' not in dot
    # intra-statement dashed edges d1 -> d2 and d3 -> d2 inside the box
    assert f'"{slug["d1"]}" -> "{slug["d2"]}"' in dot
    assert f'"{slug["d3"]}" -> "{slug["d2"]}"' in dot
    # s2 stays collapsed -> still a single node
    assert f'"{slug["s2"]}" [' in dot


def test_tier2_expand_cross_edges_use_repr_trick():
    # e1 (child of collapsed s2) depends on d2 (child of expanded s1):
    #   repr(d2) = d2 (s1 open) ; repr(e1) = s2 (collapsed) ==> d2 -> s2
    g, slug = _graph(), _slug()
    dot = rm.recolor_dot(g, _sc(), tier=2, expanded={"s1"})
    assert f'"{slug["d2"]}" -> "{slug["s2"]}"' in dot
    # the tier-2 -> tier-2 dep s1 -> s2 is gone (s1 is now its children)
    assert f'"{slug["s1"]}" -> "{slug["s2"]}"' not in dot


def test_tier3_expand_child_colored_by_color_state():
    # a rejected tier-3 decl paints the rejected trust color inside the box.
    g, slug = _graph(), _slug()
    sc = _sc({"d2": {"ai": {"faithfulness": 2, "proof_integrity": 3,
                            "code_quality": 4, "verdict": "rejected"}}})
    dot = rm.recolor_dot(g, sc, tier=2, expanded={"s1"})
    line = next(ln for ln in dot.splitlines() if f'"{slug["d2"]}" [' in ln)
    assert "rv-rejected" in line
    assert rm.VERDICT_DOT["rejected"]["color"] in line
    # d3 (in mathlib) paints blue inside the box
    line3 = next(ln for ln in dot.splitlines() if f'"{slug["d3"]}" [' in ln)
    assert "rv-in_mathlib" in line3


def test_tier3_flat_graph_renders_all_decls():
    # the flat tier-3 graph draws every tier-3 decl as a node (leaves).
    g, slug = _graph(), _slug()
    dot = rm.recolor_dot(g, _sc(), tier=3)
    for d in ("d1", "d2", "d3", "e1"):
        assert f'"{slug[d]}" [' in dot
    # tier-3 -> tier-3 deps drawn (d1 -> d2, d3 -> d2, d2 -> e1)
    assert f'"{slug["d1"]}" -> "{slug["d2"]}"' in dot
    assert f'"{slug["d2"]}" -> "{slug["e1"]}"' in dot


def test_unknown_or_leaf_expand_ignored():
    # expanding a leaf (no children) or unknown id degrades to the collapsed view.
    g, slug = _graph(), _slug()
    dot = rm.recolor_dot(g, _sc(), tier=2, expanded={"d1", "nope"})
    assert "subgraph cluster_" not in dot
    assert f'"{slug["s1"]}" [' in dot


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
