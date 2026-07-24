"""Tests for the tier-1 overview: cluster roll-up color + lifted cluster edges."""
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "scripts" / "review_ui"))
sys.path.insert(0, str(_HERE.parent / "scripts"))

import review_model as rm        # noqa: E402
import export_blueprint as eb    # noqa: E402


def _graph():
    return {
        "cA": {"id": "cA", "tier": 1, "parent": None, "kind": "section"},
        "cB": {"id": "cB", "tier": 1, "parent": None, "kind": "section"},
        "a1": {"id": "a1", "tier": 2, "parent": "cA", "kind": "definition",
               "mathlib_status": "exists", "depends_on": []},
        "a2": {"id": "a2", "tier": 2, "parent": "cA", "kind": "theorem",
               "mathlib_status": "exists", "depends_on": ["a1"]},
        "b1": {"id": "b1", "tier": 2, "parent": "cB", "kind": "theorem",
               "mathlib_status": "missing", "depends_on": ["a1"]},
    }


def _sc(reviews=None):
    sc = rm.empty_sidecar()
    sc["reviews"] = reviews or {}
    return sc


def test_cluster_all_in_mathlib_rolls_up_blue():
    assert rm.cluster_color("cA", _graph(), _sc()) == "in_mathlib"


def test_cluster_with_rejected_child_is_flagged():
    sc = _sc({"b1": {"ai": {"faithfulness": 2, "proof_integrity": 3,
                            "code_quality": 4, "verdict": "rejected"}}})
    assert rm.cluster_color("cB", _graph(), sc) == "flagged"


def test_cluster_all_clean_is_clean():
    sc = _sc({"b1": {"ai": {"faithfulness": 5, "proof_integrity": 5,
                            "code_quality": 5, "verdict": "clean"}}})
    assert rm.cluster_color("cB", _graph(), sc) == "clean"


def test_empty_cluster_is_unreviewed():
    g = {"cX": {"id": "cX", "tier": 1, "parent": None, "kind": "section"}}
    assert rm.cluster_color("cX", g, _sc()) == "unreviewed"


def test_tier1_dot_lifts_cross_cluster_edge_and_omits_tier2_nodes():
    g = _graph()
    dot = rm.recolor_dot(g, _sc(), tier=1)
    slug = eb.build_slug_map(g)
    # b1 (cB) depends on a1 (cA) -> a cluster-level edge cA -> cB
    assert f'"{slug["cA"]}" -> "{slug["cB"]}"' in dot
    # tier-2 nodes are NOT drawn as nodes in the tier-1 overview
    assert f'"{slug["a1"]}" [' not in dot
    assert f'"{slug["b1"]}" [' not in dot


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
