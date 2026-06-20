"""Tests for the N-tier server surface (serve_review): tier resolution, the
focus payload, the bootstrap globals, and the tier toggle.

These exercise ``render_home`` / ``_parse_tier`` / ``_focus_payload`` /
``_tiertoggle_html`` directly against a tiny on-disk graph (no socket needed).
"""
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "scripts" / "review_ui"))
sys.path.insert(0, str(_HERE.parent / "scripts"))

import serve_review as sv   # noqa: E402


GRAPH = {
    "metadata": {"title": "t"},
    "nodes": [
        {"id": "cA", "tier": 1, "parent": None, "kind": "section", "name": "Cl A"},
        {"id": "s1", "tier": 2, "parent": "cA", "kind": "lemma", "name": "Stmt 1",
         "mathlib_status": "missing", "depends_on": []},
        {"id": "s2", "tier": 2, "parent": "cA", "kind": "theorem", "name": "Stmt 2",
         "mathlib_status": "missing", "depends_on": ["s1"]},
        {"id": "d1", "tier": 3, "parent": "s1", "kind": "lemma", "name": "ns.d1",
         "mathlib_status": "missing", "depends_on": []},
        {"id": "d2", "tier": 3, "parent": "s1", "kind": "theorem", "name": "ns.d2",
         "mathlib_status": "missing", "depends_on": ["d1"]},
    ],
}


def _proj(tmp_path):
    gp = tmp_path / "graph.json"
    gp.write_text(json.dumps(GRAPH))
    return sv.Project(gp)


def _boot(html: bytes) -> str:
    """Return the inline bootstrap <script> contents from a rendered page."""
    text = html.decode("utf-8")
    # the boot script is the one assigning window.__RV_* globals
    start = text.index("window.__RV_DOT__")
    end = text.index("</script>", start)
    return text[start:end]


# --- tier resolution --------------------------------------------------------

def test_parse_tier_defaults_to_lowest_present(tmp_path):
    nodes = _proj(tmp_path).nodes()
    assert sv._parse_tier(None, nodes) == 1     # lowest present
    assert sv._parse_tier("3", nodes) == 3      # present -> honoured
    assert sv._parse_tier("9", nodes) == 1      # not present -> default
    assert sv._parse_tier("bad", nodes) == 1    # non-int -> default


# --- focus payload ----------------------------------------------------------

def test_focus_payload_lists_children_one_tier_down(tmp_path):
    nodes = _proj(tmp_path).nodes()
    fp = sv._focus_payload("s1", nodes)
    assert fp == {"parent": "s1", "label": "Stmt 1", "members": ["d1", "d2"]}
    # tier-1 parent focuses its tier-2 children
    fp1 = sv._focus_payload("cA", nodes)
    assert fp1["members"] == ["s1", "s2"]


def test_focus_payload_none_for_unknown_or_empty(tmp_path):
    nodes = _proj(tmp_path).nodes()
    assert sv._focus_payload(None, nodes) is None
    assert sv._focus_payload("nope", nodes) is None


# --- home bootstrap globals -------------------------------------------------

def test_home_default_tier_and_tiers_present(tmp_path):
    html = sv.render_home(_proj(tmp_path), None, None)
    boot = _boot(html)
    assert "window.__RV_TIER__ = 1;" in boot                 # default = lowest
    assert "window.__RV_TIERS__ = [1, 2, 3];" in boot        # all present
    assert "window.__RV_FOCUS__ = null;" in boot             # no focus


def test_home_tier3_renders(tmp_path):
    html = sv.render_home(_proj(tmp_path), "3", None)
    boot = _boot(html)
    assert "window.__RV_TIER__ = 3;" in boot


def test_home_focus_payload_in_boot(tmp_path):
    html = sv.render_home(_proj(tmp_path), "2", "s1")
    boot = _boot(html)
    assert "window.__RV_TIER__ = 2;" in boot
    # the focus payload is serialized with the members one tier down
    assert '"parent": "s1"' in boot
    assert '"members": ["d1", "d2"]' in boot
    assert '"label": "Stmt 1"' in boot


# --- tier toggle ------------------------------------------------------------

def test_tiertoggle_lists_present_tiers_with_spec_labels(tmp_path):
    nodes = _proj(tmp_path).nodes()
    import review_model as rm
    present = rm.tiers_present(nodes)
    html = sv._tiertoggle_html(present, 2)
    assert "1 · clusters" in html
    assert "2 · statements" in html
    assert "3 · declarations" in html
    # current tier (2) is a static span, the others are links
    assert "<span class='rv-tt rv-tt-on'>2 · statements</span>" in html
    assert "href='/?tier=1'" in html
    assert "href='/?tier=3'" in html


def test_tiertoggle_only_present_tiers(tmp_path):
    # a graph with only tiers 1 and 2 must not show a tier-3 entry
    html = sv._tiertoggle_html([1, 2], 1)
    assert "1 · clusters" in html
    assert "2 · statements" in html
    assert "declarations" not in html
    assert "href='/?tier=3'" not in html


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
