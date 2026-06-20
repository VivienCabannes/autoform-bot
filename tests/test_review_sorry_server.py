"""Server-side tests for the live ``sorry`` scan: ``Project.sorry_set``.

Covers the file→module mapping, whole-word + ``--``-comment-aware detection, the
explicit ``lean_file`` override, ancestor (unit/cluster) propagation up the
``parent`` chain, the no-``lean_root`` inert case, metadata fallback, and graceful
degradation — exercising ``serve_review.Project`` directly against a tiny on-disk
Lean tree (no socket needed).

CRITICAL invariant: with no ``lean_root`` configured the feature is inert
(``sorry_set`` is empty), so the whole review surface reproduces its pre-sorry
behavior exactly.
"""
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "scripts" / "review_ui"))
sys.path.insert(0, str(_HERE.parent / "scripts"))

import serve_review as sv  # noqa: E402


def _graph(tmp_path: Path, metadata=None, extra_nodes=None) -> Path:
    nodes = [
        {"id": "cl", "tier": 1, "parent": None, "kind": "cluster", "name": "Cl"},
        {"id": "A.B", "tier": 2, "parent": "cl", "kind": "unit", "name": "Unit AB"},
        {"id": "A.B.C", "tier": 3, "parent": "A.B", "kind": "module",
         "name": "A.B.C"},
        {"id": "A.B.D", "tier": 3, "parent": "A.B", "kind": "module",
         "name": "A.B.D"},
    ]
    if extra_nodes:
        nodes.extend(extra_nodes)
    graph = {"metadata": metadata or {}, "nodes": nodes}
    gp = tmp_path / "graph.json"
    gp.write_text(json.dumps(graph))
    return gp


def _lean(tmp_path: Path) -> Path:
    root = tmp_path / "lean"
    (root / "A" / "B").mkdir(parents=True)
    return root


def test_file_maps_to_module_and_propagates_to_ancestors(tmp_path):
    root = _lean(tmp_path)
    (root / "A" / "B" / "C.lean").write_text("theorem c := by\n  sorry\n")
    (root / "A" / "B" / "D.lean").write_text("theorem d := by trivial\n")
    proj = sv.Project(_graph(tmp_path))
    proj.lean_root = root.resolve()
    ss = proj.sorry_set(proj.nodes())
    # A/B/C.lean -> A.B.C, and its ancestors A.B (unit) + cl (cluster).
    assert ss == {"A.B.C", "A.B", "cl"}
    # The clean sibling module is not flagged.
    assert "A.B.D" not in ss


def test_admit_and_sorryax_detected(tmp_path):
    root = _lean(tmp_path)
    (root / "A" / "B" / "C.lean").write_text("def c := by admit\n")
    (root / "A" / "B" / "D.lean").write_text("def d := sorryAx _\n")
    proj = sv.Project(_graph(tmp_path))
    proj.lean_root = root.resolve()
    ss = proj.sorry_set(proj.nodes())
    assert {"A.B.C", "A.B.D"} <= ss


def test_sorry_in_line_comment_does_not_count(tmp_path):
    root = _lean(tmp_path)
    (root / "A" / "B" / "C.lean").write_text(
        "theorem c := by trivial\n-- TODO: sorry, replace later\n")
    (root / "A" / "B" / "D.lean").write_text("theorem d := by trivial\n")
    proj = sv.Project(_graph(tmp_path))
    proj.lean_root = root.resolve()
    assert proj.sorry_set(proj.nodes()) == set()


def test_whole_word_only(tmp_path):
    # `sorryHandler` / `my_admit` must not trip the whole-word matcher.
    root = _lean(tmp_path)
    (root / "A" / "B" / "C.lean").write_text(
        "def sorryHandler := 1\ndef my_admit := 2\n")
    proj = sv.Project(_graph(tmp_path))
    proj.lean_root = root.resolve()
    assert proj.sorry_set(proj.nodes()) == set()


def test_explicit_lean_file_overrides_path(tmp_path):
    root = _lean(tmp_path)
    (root / "pinned.lean").write_text("def p := by sorry\n")
    extra = [{"id": "pinNode", "tier": 3, "parent": "A.B", "kind": "module",
              "name": "pin", "lean_file": "pinned.lean"}]
    proj = sv.Project(_graph(tmp_path, extra_nodes=extra))
    proj.lean_root = root.resolve()
    ss = proj.sorry_set(proj.nodes())
    # The node id (not the dotted path "pinned") is used, plus its ancestors.
    assert ss == {"pinNode", "A.B", "cl"}


def test_orphan_lean_file_contributes_no_phantom_id(tmp_path):
    root = _lean(tmp_path)
    # A .lean with no corresponding graph node must add nothing.
    (root / "A" / "Orphan.lean").write_text("example := by sorry\n")
    proj = sv.Project(_graph(tmp_path))
    proj.lean_root = root.resolve()
    assert proj.sorry_set(proj.nodes()) == set()


def test_no_lean_root_is_inert(tmp_path):
    root = _lean(tmp_path)
    (root / "A" / "B" / "C.lean").write_text("theorem c := by sorry\n")
    proj = sv.Project(_graph(tmp_path))  # lean_root left unset
    assert proj.sorry_set(proj.nodes()) == set()


def test_metadata_lean_root_fallback(tmp_path):
    root = _lean(tmp_path)
    (root / "A" / "B" / "C.lean").write_text("theorem c := by sorry\n")
    proj = sv.Project(_graph(tmp_path, metadata={"lean_root": str(root.resolve())}))
    # lean_root unset on the Project; resolved from metadata.lean_root.
    assert proj.sorry_set(proj.nodes()) == {"A.B.C", "A.B", "cl"}


def test_missing_lean_root_dir_degrades_to_empty(tmp_path):
    proj = sv.Project(_graph(tmp_path))
    proj.lean_root = (tmp_path / "does_not_exist").resolve()
    assert proj.sorry_set(proj.nodes()) == set()


def test_parent_cycle_terminates(tmp_path):
    # A malformed parent cycle must not hang the ancestor walk.
    root = _lean(tmp_path)
    (root / "A" / "B" / "C.lean").write_text("theorem c := by sorry\n")
    # A.B's parent points back at its own descendant A.B.C (a cycle).
    nodes = [
        {"id": "A.B", "tier": 2, "parent": "A.B.C", "kind": "unit"},
        {"id": "A.B.C", "tier": 3, "parent": "A.B", "kind": "module"},
    ]
    gp = tmp_path / "graph.json"
    gp.write_text(json.dumps({"metadata": {}, "nodes": nodes}))
    proj = sv.Project(gp)
    proj.lean_root = root.resolve()
    ss = proj.sorry_set(proj.nodes())
    # Both members of the cycle are reached once; the walk terminates.
    assert ss == {"A.B.C", "A.B"}


def test_lean_has_sorry_ignores_comments_and_hyphen_form():
    """Regression: the detector must not trip on prose. A `sorry`/`admit` inside a
    `--` or `/- … -/` comment, or the hyphenated word form `sorry-free`, is NOT a
    real gap; only a bare `sorry`/`admit`/`sorryAx` token in code counts."""
    has = sv._lean_has_sorry
    # prose / comments / identifiers → not flagged
    assert has("-- TODO sorry later") is False
    assert has("/- a sorry in a block -/") is False
    assert has("/-! inspection-verified sorry-free at port -/\ndef f := 1") is False
    assert has("inspection-verified sorry-free") is False
    assert has("def sorryHandler := 1") is False
    assert has("structure my_admit where x : Nat") is False
    # real incompleteness → flagged
    assert has("  exact sorry") is True
    assert has("theorem t : p := by admit") is True
    assert has("  := sorryAx _") is True
