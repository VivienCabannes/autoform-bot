"""Tests for the "sorry / not implemented" node state on the review surface.

A node is ``sorry`` (violet — ``#7C3AED`` border / ``#ECE7FB`` fill) when its Lean
contains a whole-word ``sorry``/``admit``/``sorryAx`` (or, for a parent unit/cluster,
a descendant module does). It is semantically distinct from a review verdict: rejected
= overclaim, sorry = an honest, incomplete gap. It is the Worker's target.

Precedence (``color_state``): **sorry** wins over every verdict (rejected / flagged),
over the blue in-Mathlib state, and over clean/unreviewed. A sorry node is **never
trusted** and **seeds taint** (its forward ``depends_on`` closure is hatched), exactly
like a flagged/rejected node.

CRITICAL invariant under test: ``sorry_set=None`` / omitted reproduces the original
behavior exactly — every threaded function falls back to an empty set.
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


# --- palette / VERDICT_DOT carry the violet ---------------------------------

def test_palette_and_verdict_dot_have_violet_sorry():
    assert rm.PALETTE["sorry"] == "#7C3AED"
    assert rm.VERDICT_DOT["sorry"] == {"color": "#7C3AED", "fill": "#ECE7FB"}


# --- color_state: sorry has TOP precedence over every other state -----------

def test_color_state_sorry_overrides_rejected():
    # rejected (overclaim) is normally red, but an incomplete Lean is the dominant
    # fact -> violet.
    assert rm.color_state({}, "rejected", is_sorry=True) == "sorry"


def test_color_state_sorry_overrides_flagged():
    assert rm.color_state({}, "flagged", is_sorry=True) == "sorry"


def test_color_state_sorry_overrides_in_mathlib():
    # an in-Mathlib node is normally blue; a sorry in it still wins -> violet.
    node = {"mathlib_status": "exists"}
    assert rm.color_state(node, "unreviewed", is_sorry=True) == "sorry"
    assert rm.color_state(node, "clean", is_sorry=True) == "sorry"


def test_color_state_sorry_overrides_clean_and_unreviewed():
    assert rm.color_state({}, "clean", is_sorry=True) == "sorry"
    assert rm.color_state({}, "unreviewed", is_sorry=True) == "sorry"


def test_color_state_default_is_sorry_false_unchanged():
    # The CRITICAL invariant at the unit level: omitting is_sorry == today's behavior.
    assert rm.color_state({"mathlib_status": "exists"}, "clean") == "in_mathlib"
    assert rm.color_state({}, "rejected") == "rejected"
    assert rm.color_state({}, "flagged") == "flagged"
    assert rm.color_state({}, "clean") == "clean"
    assert rm.color_state({}, "unreviewed") == "unreviewed"
    # passing is_sorry=False explicitly is identical to omitting it
    assert rm.color_state({}, "rejected", is_sorry=False) == "rejected"
    assert rm.color_state({"mathlib_status": "exists"}, "clean", False) == "in_mathlib"


# --- is_trusted: a sorry node is NEVER trusted ------------------------------

def test_is_trusted_false_for_sorry_node_even_if_clean():
    node = {"mathlib_status": "missing"}
    sc = _sidecar({"a": {"human": {"verdict": "clean"}}})
    # clean -> normally trusted, but a sorry overrides.
    assert rm.is_trusted("a", node, sc) is True
    assert rm.is_trusted("a", node, sc, sorry_set={"a"}) is False


def test_is_trusted_false_for_sorry_node_even_if_in_mathlib():
    node = {"mathlib_status": "exists"}
    sc = rm.empty_sidecar()
    # in-Mathlib + no defect -> normally trusted, but a sorry overrides.
    assert rm.is_trusted("a", node, sc) is True
    assert rm.is_trusted("a", node, sc, sorry_set={"a"}) is False


def test_is_trusted_unaffected_for_node_not_in_sorry_set():
    node = {"mathlib_status": "missing"}
    sc = _sidecar({"a": {"human": {"verdict": "clean"}}})
    # a different node being sorry must not touch "a".
    assert rm.is_trusted("a", node, sc, sorry_set={"b"}) is True


def test_is_trusted_default_none_unchanged():
    node = {"mathlib_status": "missing"}
    sc = _sidecar({"a": {"human": {"verdict": "clean"}}})
    assert rm.is_trusted("a", node, sc) == rm.is_trusted("a", node, sc, None)
    assert rm.is_trusted("a", node, sc) is True


# --- tainted_set: sorry nodes are taint sources -----------------------------

def test_tainted_set_sorry_taints_forward_closure():
    # B depends on A; C depends on B. A is a sorry -> B and C are tainted.
    nodes = {
        "A": {"tier": 2, "depends_on": []},
        "B": {"tier": 2, "depends_on": ["A"]},
        "C": {"tier": 2, "depends_on": ["B"]},
    }
    sc = rm.empty_sidecar()
    # no verdicts at all: without sorry, nothing taints.
    assert rm.tainted_set(nodes, sc) == set()
    # A sorry -> its forward depends_on closure (B, C) is tainted; A itself is the
    # source, not a victim, so it is NOT in the tainted set.
    assert rm.tainted_set(nodes, sc, sorry_set={"A"}) == {"B", "C"}


def test_tainted_set_sources_are_flagged_union_rejected_union_sorry():
    # A flagged, X sorry; both seed taint. B (deps A) and Y (deps X) tainted.
    nodes = {
        "A": {"tier": 2, "depends_on": []},
        "B": {"tier": 2, "depends_on": ["A"]},
        "X": {"tier": 2, "depends_on": []},
        "Y": {"tier": 2, "depends_on": ["X"]},
    }
    sc = _sidecar({"A": {"ai": {"verdict": "flagged"}}})
    assert rm.tainted_set(nodes, sc, sorry_set={"X"}) == {"B", "Y"}


def test_tainted_set_default_none_unchanged():
    nodes = {
        "A": {"tier": 2, "depends_on": []},
        "B": {"tier": 2, "depends_on": ["A"]},
    }
    sc = _sidecar({"A": {"ai": {"verdict": "rejected"}}})
    # the only source is the rejected A -> B tainted, identical with/without the arg.
    assert rm.tainted_set(nodes, sc) == {"B"}
    assert rm.tainted_set(nodes, sc) == rm.tainted_set(nodes, sc, None)


# --- _verdict_node_dot: violet + rv-sorry + SOLID ---------------------------

def test_verdict_node_dot_sorry_emits_violet_solid_class():
    line = rm._verdict_node_dot(
        "A", {"kind": "theorem"}, "A", "unreviewed", None, False, is_sorry=True)
    assert '#7C3AED' in line          # violet border
    assert '#ECE7FB' in line          # violet fill
    assert 'rv-sorry' in line         # the class the client keys on
    assert 'rv-solid' in line         # solid, not an AI-only ring
    assert 'dashed' not in line       # NOT dashed (a code fact, not AI-only)
    assert 'filled' in line


def test_verdict_node_dot_sorry_beats_state_override():
    # even a roll-up/verdict state_override yields to the sorry fact.
    line = rm._verdict_node_dot(
        "A", {}, "A", "unreviewed", None, False,
        state_override="in_mathlib", is_sorry=True)
    assert '#7C3AED' in line
    assert 'rv-sorry' in line
    assert 'rv-in_mathlib' not in line


def test_verdict_node_dot_default_not_sorry_unchanged():
    # omitting is_sorry reproduces today's output for an unreviewed leaf.
    base = rm._verdict_node_dot("A", {"kind": "theorem"}, "A", "unreviewed", None, False)
    explicit = rm._verdict_node_dot(
        "A", {"kind": "theorem"}, "A", "unreviewed", None, False, is_sorry=False)
    assert base == explicit
    assert 'rv-sorry' not in base
    assert '#7C3AED' not in base


# --- recolor_dot: a sorry node renders violet + rv-sorry --------------------

def _tier2(deps_map, kinds=None):
    kinds = kinds or {}
    return {
        nid: {"tier": 2, "depends_on": deps, "kind": kinds.get(nid, "theorem")}
        for nid, deps in deps_map.items()
    }


def test_recolor_dot_emits_violet_sorry_node():
    nodes = _tier2({"A": [], "B": ["A"]})
    sc = rm.empty_sidecar()
    dot = rm.recolor_dot(nodes, sc, tier=2, sorry_set={"A"})
    # the sorry node A is violet with class rv-sorry
    assert '#7C3AED' in dot
    assert '#ECE7FB' in dot
    assert 'rv-sorry' in dot


def test_recolor_dot_default_none_has_no_violet():
    nodes = _tier2({"A": [], "B": ["A"]})
    sc = rm.empty_sidecar()
    assert rm.recolor_dot(nodes, sc, tier=2) == rm.recolor_dot(
        nodes, sc, tier=2, sorry_set=None)
    base = rm.recolor_dot(nodes, sc, tier=2)
    assert 'rv-sorry' not in base
    assert '#7C3AED' not in base


# --- compute_state: colors + taint + payload + None invariant ---------------

def test_compute_state_taints_sorry_forward_closure():
    nodes = {
        "A": {"tier": 2, "depends_on": []},
        "B": {"tier": 2, "depends_on": ["A"]},
        "C": {"tier": 2, "depends_on": ["B"]},
    }
    sc = rm.empty_sidecar()
    state = rm.compute_state(nodes, sc, sorry_set={"A"})
    # A is the violet source; B and C are tainted (downstream of the gap).
    assert state["colors"]["A"] == "sorry"
    assert state["tainted"] == ["B", "C"]
    assert state["sorry"] == ["A"]


def test_compute_state_sorry_color_overrides_verdict_and_blue():
    nodes = {
        "A": {"tier": 2, "mathlib_status": "exists", "depends_on": []},
        "B": {"tier": 2, "depends_on": []},
    }
    # A would be blue (in-mathlib); B would be rejected (red). Both are sorry -> violet.
    sc = _sidecar({"B": {"ai": {"verdict": "rejected"}}})
    state = rm.compute_state(nodes, sc, sorry_set={"A", "B"})
    assert state["colors"]["A"] == "sorry"
    assert state["colors"]["B"] == "sorry"
    # raw verdicts unchanged (color is a separate axis)
    assert state["verdicts"]["B"] == "rejected"


def test_compute_state_sorry_node_off_frontier_and_not_trusted():
    # sink C rests on A (clean) and B (clean) -> normally on the frontier;
    # marking A sorry knocks C off and drops the trusted count.
    nodes = {
        "A": {"tier": 2, "depends_on": []},
        "B": {"tier": 2, "depends_on": ["A"]},
        "C": {"tier": 2, "depends_on": ["B"]},
    }
    sc = _sidecar({
        "A": {"human": {"verdict": "clean"}},
        "B": {"human": {"verdict": "clean"}},
        "C": {"human": {"verdict": "clean"}},
    })
    base = rm.compute_state(nodes, sc)
    assert base["trust_frontier"] == ["C"]
    assert base["coverage"]["trusted"] == 3

    withsorry = rm.compute_state(nodes, sc, sorry_set={"A"})
    assert withsorry["trust_frontier"] == []          # closure touches the gap
    assert withsorry["coverage"]["trusted"] == 2       # A no longer trusted


def test_compute_state_default_none_identical_to_current():
    # THE critical invariant: sorry_set=None / omitted == today's behavior.
    nodes = {
        "A": {"tier": 1, "depends_on": []},
        "S": {"tier": 2, "parent": "A", "mathlib_status": "exists", "depends_on": []},
        "T": {"tier": 2, "parent": "A", "depends_on": ["S"]},
        "U": {"tier": 2, "parent": "A", "depends_on": ["T"]},
    }
    sc = _sidecar({"T": {"ai": {"verdict": "flagged"}}})
    omitted = rm.compute_state(nodes, sc)
    explicit_none = rm.compute_state(nodes, sc, None)
    explicit_empty = rm.compute_state(nodes, sc, set())
    # the only addition is the empty "sorry" key; everything else is byte-identical.
    assert omitted == explicit_none == explicit_empty
    assert omitted["sorry"] == []
    # and the rest of the payload is exactly what it was before the feature:
    assert omitted["colors"]["S"] == "in_mathlib"   # blue, not violet
    assert omitted["tainted"] == ["U"]              # flagged T taints U only
    assert "sorry" not in omitted["colors"].values()


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
