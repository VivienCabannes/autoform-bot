"""Tests for in-place tier-1 cluster expansion in ``recolor_dot``.

Covers the collapsed-vs-expanded DOT: a collapsed cluster is a single node with
lifted cross-cluster edges; an expanded cluster emits a ``subgraph cluster_<slug>``
box holding its tier-2 children + intra-cluster dashed edges, and cross edges are
emitted via the repr() trick (child if its cluster is expanded, else the cluster
node), deduped.
"""
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "scripts" / "review_ui"))
sys.path.insert(0, str(_HERE.parent / "scripts"))

import review_model as rm        # noqa: E402
import export_blueprint as eb    # noqa: E402


def _graph():
    # Two clusters. cA holds a1 -> a2 (intra). cB holds b1, b2; b1 depends on a1
    # (cross A->B); b2 depends on a2 (cross A->B); a2 also has no further deps.
    return {
        "cA": {"id": "cA", "tier": 1, "parent": None, "kind": "section",
               "name": "Cluster A"},
        "cB": {"id": "cB", "tier": 1, "parent": None, "kind": "section",
               "name": "Cluster B"},
        "a1": {"id": "a1", "tier": 2, "parent": "cA", "kind": "definition",
               "mathlib_status": "missing", "depends_on": []},
        "a2": {"id": "a2", "tier": 2, "parent": "cA", "kind": "theorem",
               "mathlib_status": "missing", "depends_on": ["a1"]},
        "b1": {"id": "b1", "tier": 2, "parent": "cB", "kind": "theorem",
               "mathlib_status": "missing", "depends_on": ["a1"]},
        "b2": {"id": "b2", "tier": 2, "parent": "cB", "kind": "theorem",
               "mathlib_status": "missing", "depends_on": ["a2", "b1"]},
    }


def _sc(reviews=None):
    sc = rm.empty_sidecar()
    sc["reviews"] = reviews or {}
    return sc


def _slug():
    return eb.build_slug_map(_graph())


# --- collapsed (no expansion) -----------------------------------------------

def test_collapsed_has_no_subgraph_and_cluster_nodes_present():
    g, slug = _graph(), _slug()
    dot = rm.recolor_dot(g, _sc(), tier=1)  # expanded defaults to None
    assert "subgraph cluster_" not in dot
    assert f'"{slug["cA"]}" [' in dot
    assert f'"{slug["cB"]}" [' in dot
    # tier-2 children are NOT drawn when both clusters are collapsed
    assert f'"{slug["a1"]}" [' not in dot
    assert f'"{slug["b1"]}" [' not in dot


def test_collapsed_lifts_cross_cluster_edge():
    g, slug = _graph(), _slug()
    dot = rm.recolor_dot(g, _sc(), tier=1)
    # cross deps a1<-b1, a2<-b2 both lift to a single cA -> cB edge (deduped)
    assert f'"{slug["cA"]}" -> "{slug["cB"]}"' in dot
    assert dot.count(f'"{slug["cA"]}" -> "{slug["cB"]}"') == 1


# --- expanded ----------------------------------------------------------------

def test_expanded_emits_subgraph_with_children_and_intra_edge():
    g, slug = _graph(), _slug()
    dot = rm.recolor_dot(g, _sc(), tier=1, expanded={"cA"})
    # the cluster box, labelled by the cluster name
    assert f"subgraph cluster_{slug['cA']} {{" in dot
    assert 'label="Cluster A";' in dot
    # children are drawn as nodes; the cluster itself is NOT a single node
    assert f'"{slug["a1"]}" [' in dot
    assert f'"{slug["a2"]}" [' in dot
    assert f'"{slug["cA"]}" [' not in dot
    # intra-cluster edge a1 -> a2 inside the box
    assert f'"{slug["a1"]}" -> "{slug["a2"]}"' in dot


def test_expanded_cross_edges_use_repr_trick():
    g, slug = _graph(), _slug()
    dot = rm.recolor_dot(g, _sc(), tier=1, expanded={"cA"})
    # cB stays collapsed -> still a node
    assert f'"{slug["cB"]}" [' in dot
    # edges flow dep -> dependent. b1 dep a1 (a1's cluster cA expanded) =>
    # repr(a1)=a1 (child) -> repr(b1)=cB  ==>  a1 -> cB
    assert f'"{slug["a1"]}" -> "{slug["cB"]}"' in dot
    # b2 dep a2 => repr(a2)=a2 -> repr(b2)=cB  ==>  a2 -> cB
    assert f'"{slug["a2"]}" -> "{slug["cB"]}"' in dot
    # no lifted cA -> cB edge anymore (cA's endpoints are now their own children)
    assert f'"{slug["cA"]}" -> "{slug["cB"]}"' not in dot
    assert f'"{slug["cB"]}" -> "{slug["cA"]}"' not in dot


def test_both_expanded_no_cluster_nodes_all_children_drawn():
    g, slug = _graph(), _slug()
    dot = rm.recolor_dot(g, _sc(), tier=1, expanded={"cA", "cB"})
    for cid in ("cA", "cB"):
        assert f"subgraph cluster_{slug[cid]} {{" in dot
        assert f'"{slug[cid]}" [' not in dot
    for child in ("a1", "a2", "b1", "b2"):
        assert f'"{slug[child]}" [' in dot
    # every tier-2 dep is now a child->child edge: b1 dep a1 => b1's repr a1
    assert f'"{slug["a1"]}" -> "{slug["b1"]}"' in dot
    assert f'"{slug["a2"]}" -> "{slug["b2"]}"' in dot
    assert f'"{slug["b1"]}" -> "{slug["b2"]}"' in dot
    # intra cA edge still present
    assert f'"{slug["a1"]}" -> "{slug["a2"]}"' in dot


def test_expanded_children_colored_by_color_state():
    # a rejected child should paint the rejected trust color inside the box.
    g, slug = _graph(), _slug()
    sc = _sc({"b1": {"ai": {"faithfulness": 2, "proof_integrity": 3,
                            "code_quality": 4, "verdict": "rejected"}}})
    dot = rm.recolor_dot(g, sc, tier=1, expanded={"cB"})
    # b1's node line carries the rejected fill/class (color_state honoured)
    line = next(ln for ln in dot.splitlines() if f'"{slug["b1"]}" [' in ln)
    assert "rv-rejected" in line
    assert rm.VERDICT_DOT["rejected"]["color"] in line


def test_unknown_expanded_id_ignored():
    # expanding a non-tier-1 id (or a missing id) degrades to the collapsed view.
    g, slug = _graph(), _slug()
    dot = rm.recolor_dot(g, _sc(), tier=1, expanded={"a1", "does_not_exist"})
    assert "subgraph cluster_" not in dot
    assert f'"{slug["cA"]}" [' in dot


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
