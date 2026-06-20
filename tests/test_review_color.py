"""Tests for the "in Mathlib = blue" trust state on the review surface.

Color = trust state, not the raw verdict:
  * blue  ``in_mathlib`` — node already in Mathlib (reused, trusted by construction),
  * green ``clean``      — ours, reviewed clean,
  * amber ``flagged`` / red ``rejected`` — a real defect, overrides blue,
  * grey  ``unreviewed`` — ours, not yet reviewed.

A blue node is **trusted** for taint/frontier/coverage (it is never "unreviewed"),
but a flagged/rejected verdict still overrides to amber/red even on a Mathlib node.
"""
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "scripts" / "review_ui"))
sys.path.insert(0, str(_HERE.parent / "scripts"))

import review_model as rm  # noqa: E402


def _sidecar(reviews):
    sc = rm.empty_sidecar()
    sc["reviews"] = reviews
    return sc


# --- is_in_mathlib: canonical "exists" + tolerated spellings ---------------

def test_is_in_mathlib_recognizes_canonical_and_aliases():
    assert rm.is_in_mathlib({"mathlib_status": "exists"})
    assert rm.is_in_mathlib({"mathlib_status": "in-mathlib"})
    assert rm.is_in_mathlib({"mathlib_status": "in_mathlib"})
    assert rm.is_in_mathlib({"mathlib_status": "mathlib"})
    assert not rm.is_in_mathlib({"mathlib_status": "missing"})
    assert not rm.is_in_mathlib({"mathlib_status": "partial"})
    assert not rm.is_in_mathlib({})  # no status -> not in mathlib


# --- color_state: the exact rule -------------------------------------------

def test_color_state_exists_clean_is_blue():
    # already in Mathlib + reviewed clean -> blue (not green): reuse, not ours.
    assert rm.color_state({"mathlib_status": "exists"}, "clean") == "in_mathlib"


def test_color_state_exists_unreviewed_is_blue():
    # already in Mathlib + nobody reviewed -> blue (NOT grey): trusted by construction.
    assert rm.color_state({"mathlib_status": "exists"}, "unreviewed") == "in_mathlib"


def test_color_state_exists_rejected_is_red_defect_overrides():
    # a real defect (e.g. wrong Mathlib lemma cited) shows even on a reuse.
    assert rm.color_state({"mathlib_status": "exists"}, "rejected") == "rejected"


def test_color_state_exists_flagged_is_amber_defect_overrides():
    assert rm.color_state({"mathlib_status": "exists"}, "flagged") == "flagged"


def test_color_state_missing_clean_is_green():
    assert rm.color_state({"mathlib_status": "missing"}, "clean") == "clean"


def test_color_state_missing_unreviewed_is_grey():
    assert rm.color_state({"mathlib_status": "missing"}, "unreviewed") == "unreviewed"


def test_color_state_no_status_falls_through_to_verdict():
    assert rm.color_state({}, "clean") == "clean"
    assert rm.color_state({}, "unreviewed") == "unreviewed"


# --- is_trusted: blue counts as trusted ------------------------------------

def test_is_trusted_blue_counts_as_trusted():
    node = {"mathlib_status": "exists"}
    # in Mathlib + no defect -> trusted even with no review at all.
    assert rm.is_trusted("a", node, rm.empty_sidecar())


def test_is_trusted_blue_with_defect_is_not_trusted():
    node = {"mathlib_status": "exists"}
    sc = _sidecar({"a": {"human": {"verdict": "rejected"}}})
    assert not rm.is_trusted("a", node, sc)


def test_is_trusted_ours_clean_is_trusted():
    node = {"mathlib_status": "missing"}
    sc = _sidecar({"a": {"human": {"verdict": "clean"}}})
    assert rm.is_trusted("a", node, sc)


def test_is_trusted_ours_unreviewed_is_not_trusted():
    node = {"mathlib_status": "missing"}
    assert not rm.is_trusted("a", node, rm.empty_sidecar())


# --- trust_frontier: a sink on an all-in-Mathlib + clean closure is in -----

def test_trust_frontier_blue_closure_counts_as_trusted():
    # sink "C" depends on "B" (in Mathlib, unreviewed) and "A" (in Mathlib, clean);
    # nothing is ours-unreviewed, so the whole closure is trusted -> C on frontier.
    nodes = {
        "A": {"tier": 2, "mathlib_status": "exists", "depends_on": []},
        "B": {"tier": 2, "mathlib_status": "exists", "depends_on": ["A"]},
        "C": {"tier": 2, "mathlib_status": "exists", "depends_on": ["B"]},
    }
    sc = _sidecar({"A": {"human": {"verdict": "clean"}}})
    assert rm.trust_frontier(nodes, sc) == ["C"]


def test_trust_frontier_mixed_blue_and_clean_closure():
    # C (ours, clean) rests on B (in Mathlib) which rests on A (in Mathlib).
    nodes = {
        "A": {"tier": 2, "mathlib_status": "exists", "depends_on": []},
        "B": {"tier": 2, "mathlib_status": "in-mathlib", "depends_on": ["A"]},
        "C": {"tier": 2, "mathlib_status": "missing", "depends_on": ["B"]},
    }
    sc = _sidecar({"C": {"human": {"verdict": "clean"}}})
    assert rm.trust_frontier(nodes, sc) == ["C"]


def test_trust_frontier_breaks_on_ours_unreviewed_in_closure():
    # B is ours + unreviewed -> not trusted -> C not on frontier.
    nodes = {
        "A": {"tier": 2, "mathlib_status": "exists", "depends_on": []},
        "B": {"tier": 2, "mathlib_status": "missing", "depends_on": ["A"]},
        "C": {"tier": 2, "mathlib_status": "exists", "depends_on": ["B"]},
    }
    assert rm.trust_frontier(nodes, rm.empty_sidecar()) == []


def test_trust_frontier_breaks_on_defect_even_in_mathlib():
    # A is in Mathlib but flagged (a real defect) -> not trusted -> C not on frontier.
    nodes = {
        "A": {"tier": 2, "mathlib_status": "exists", "depends_on": []},
        "C": {"tier": 2, "mathlib_status": "exists", "depends_on": ["A"]},
    }
    sc = _sidecar({"A": {"human": {"verdict": "flagged"}}})
    assert rm.trust_frontier(nodes, sc) == []


# --- coverage: in_mathlib + trusted counts ---------------------------------

def test_coverage_counts_in_mathlib_and_trusted():
    nodes = {
        "A": {"tier": 2, "mathlib_status": "exists", "depends_on": []},
        "B": {"tier": 2, "mathlib_status": "missing", "depends_on": []},
        "C": {"tier": 2, "mathlib_status": "exists", "depends_on": []},
    }
    sc = _sidecar({"B": {"human": {"verdict": "clean"}}})
    cov = rm.coverage(nodes, sc)
    assert cov["total"] == 3
    assert cov["in_mathlib"] == 2          # A and C
    # trusted: A (blue), C (blue), B (ours-clean) -> all 3.
    assert cov["trusted"] == 3
    # still keeps the original keys.
    assert set(cov) >= {"total", "reviewed", "human_confirmed", "fraction"}


def test_in_mathlib_node_does_not_taint_downstream():
    # A in Mathlib (unreviewed) must NOT taint its dependents.
    nodes = {
        "A": {"tier": 2, "mathlib_status": "exists", "depends_on": []},
        "B": {"tier": 2, "mathlib_status": "missing", "depends_on": ["A"]},
    }
    assert rm.tainted_set(nodes, rm.empty_sidecar()) == set()


# --- compute_state exposes the colors map ----------------------------------

def test_compute_state_has_colors_map_with_blue():
    nodes = {
        "A": {"tier": 2, "mathlib_status": "exists", "depends_on": []},
        "B": {"tier": 2, "mathlib_status": "missing", "depends_on": []},
    }
    sc = _sidecar({"B": {"human": {"verdict": "clean"}}})
    state = rm.compute_state(nodes, sc)
    assert "colors" in state
    assert state["colors"]["A"] == "in_mathlib"   # blue
    assert state["colors"]["B"] == "clean"        # green
    # verdicts still raw (A is unreviewed as a verdict, but blue as a color).
    assert state["verdicts"]["A"] == "unreviewed"


if __name__ == "__main__":
    # pytest may be absent: run every test_* function directly.
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"ERROR {name}: {exc!r}")
    print(f"\n{'OK' if not failures else f'{failures} FAILED'}")
    raise SystemExit(1 if failures else 0)
