"""Tests for the local/neighborhood server surface (serve_review).

Covers the bounded focus view, the anchor view with radius clamping, the flat
too-large guard (placeholder DOT + picker payload), the render_home hooks/containers,
and the /api/dot mirror of focus/anchor/too-large.
"""
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "scripts" / "review_ui"))
sys.path.insert(0, str(_HERE.parent / "scripts"))

import serve_review as sv   # noqa: E402
import review_model as rm   # noqa: E402


# A small graph with a tier-2 unit s1 and several tier-3 modules under it, plus a
# WIDE tier-3 (> LARGE) so the flat tier-3 view is "too large".
def _build_graph(n_tier3=8):
    nodes = [
        {"id": "cA", "tier": 1, "parent": None, "kind": "section", "name": "Cl A"},
        {"id": "s1", "tier": 2, "parent": "cA", "kind": "lemma", "name": "Stmt 1",
         "mathlib_status": "missing", "depends_on": []},
        {"id": "s2", "tier": 2, "parent": "cA", "kind": "theorem", "name": "Stmt 2",
         "mathlib_status": "missing", "depends_on": ["s1"]},
    ]
    # tier-3 chain m0 -> m1 -> ... all parented to s1
    for i in range(n_tier3):
        nodes.append({
            "id": f"m{i}", "tier": 3, "parent": "s1", "kind": "lemma",
            "name": f"ns.m{i}", "mathlib_status": "missing",
            "depends_on": [f"m{i-1}"] if i else [],
        })
    return {"metadata": {"title": "t"}, "nodes": nodes}


def _proj(tmp_path, n_tier3=8):
    gp = tmp_path / "graph.json"
    gp.write_text(json.dumps(_build_graph(n_tier3)))
    return sv.Project(gp)


def _boot(html: bytes) -> str:
    text = html.decode("utf-8")
    start = text.index("window.__RV_DOT__")
    end = text.index("</script>", start)
    return text[start:end]


def _dot(html: bytes):
    """Pull the JSON-encoded __RV_DOT__ value back out of the boot script."""
    boot = _boot(html)
    after = boot.split("window.__RV_DOT__ = ", 1)[1]
    # the value is a JSON string literal terminated by `;window.__RV_STATE__`
    raw = after.split(";window.__RV_STATE__", 1)[0]
    return json.loads(raw)


# --- radius clamp -----------------------------------------------------------

def test_clamp_radius_bounds_1_to_3():
    assert sv._clamp_radius(None) == 1
    assert sv._clamp_radius("bad") == 1
    assert sv._clamp_radius("0") == 1     # clamp up
    assert sv._clamp_radius("2") == 2
    assert sv._clamp_radius("9") == 3     # clamp down
    assert sv._clamp_radius("-5") == 1


# --- anchor payload ---------------------------------------------------------

def test_anchor_payload_id_and_radius(tmp_path):
    nodes = _proj(tmp_path).nodes()
    assert sv._anchor_payload("m2", 2, nodes) == {"id": "m2", "radius": 2}
    assert sv._anchor_payload(None, 1, nodes) is None
    assert sv._anchor_payload("nope", 1, nodes) is None


# --- focus view: bounded neighborhood of a unit's children ------------------

def test_focus_renders_bounded_neighborhood(tmp_path):
    # s1 has 8 tier-3 children m0..m7; focusing s1 at tier 3 renders
    # neighborhood(children, radius=1) — bounded, contains the members.
    html = sv.render_home(_proj(tmp_path), "3", "s1")
    boot = _boot(html)
    assert "window.__RV_NEIGHBORHOOD__ = true;" in boot
    assert '"parent": "s1"' in boot          # focus payload present
    assert "window.__RV_TOO_LARGE__ = null;" in boot   # focus is never too-large
    dot = _dot(html)
    # the children are tier-3 modules; the focus dot draws them (bounded), not s1/s2.
    assert dot.count('" [') >= 1
    assert "subgraph cluster_" not in dot    # flat local view, no expansion


def test_focus_neighborhood_only_matches_model(tmp_path):
    nodes = _proj(tmp_path).nodes()
    members = rm.child_ids("s1", nodes)
    nb = rm.neighborhood(set(members), nodes, 3, radius=1, cap=sv.NB_CAP)
    expected = rm.recolor_dot(nodes, _proj(tmp_path).sidecar(), tier=3, only=nb)
    dot = _dot(sv.render_home(_proj(tmp_path), "3", "s1"))
    assert dot == expected


# --- anchor view: neighborhood of one node ±K hops --------------------------

def test_anchor_view_boots_anchor_payload(tmp_path):
    html = sv.render_home(_proj(tmp_path), "3", None, "m3", "2")
    boot = _boot(html)
    assert "window.__RV_NEIGHBORHOOD__ = true;" in boot
    assert '"id": "m3"' in boot
    assert '"radius": 2' in boot
    assert "window.__RV_TOO_LARGE__ = null;" in boot


def test_anchor_radius_is_clamped_in_boot(tmp_path):
    boot = _boot(sv.render_home(_proj(tmp_path), "3", None, "m3", "99"))
    assert '"radius": 3' in boot            # clamped to NB_MAX_RADIUS


def test_anchor_dot_contains_anchor_and_is_bounded(tmp_path):
    nodes = _proj(tmp_path, n_tier3=8).nodes()
    import export_blueprint as eb
    slug = eb.build_slug_map(nodes)
    dot = _dot(sv.render_home(_proj(tmp_path, 8), "3", None, "m3", "1"))
    assert f'"{slug["m3"]}" [' in dot       # anchor present
    # bounded to <= cap nodes
    assert dot.count('" [') <= sv.NB_CAP


# --- flat too-large guard ---------------------------------------------------

def test_flat_too_large_no_full_graph(tmp_path):
    # a flat tier-3 with > LARGE modules must NOT render the full graph.
    n = sv.LARGE + 30
    html = sv.render_home(_proj(tmp_path, n_tier3=n), "3", None)
    boot = _boot(html)
    assert f'"tier": 3' in boot
    assert f'"count": {n}' in boot
    assert f'"threshold": {sv.LARGE}' in boot
    assert "window.__RV_TOO_LARGE__ = {" in boot
    # the picker payload carries the tier's nodes (id/label/parent)
    assert "window.__RV_NODES__ = [" in boot
    assert '"id": "m0"' in boot
    assert '"parent": "s1"' in boot
    # the DOT is the empty placeholder — NOT the n-node graph
    dot = _dot(html)
    assert "__rv_placeholder__" in dot
    assert dot.count('" [') <= 1            # only the invisible placeholder node


def test_flat_small_tier_renders_whole(tmp_path):
    # a flat tier under LARGE renders normally (no guard).
    html = sv.render_home(_proj(tmp_path, n_tier3=8), "3", None)
    boot = _boot(html)
    assert "window.__RV_TOO_LARGE__ = null;" in boot
    assert "window.__RV_NODES__ = null;" in boot
    dot = _dot(html)
    assert "__rv_placeholder__" not in dot


def test_placeholder_dot_is_valid_and_empty():
    dot = sv._placeholder_dot()
    assert dot.startswith('strict digraph "" {')
    assert "__rv_placeholder__" in dot
    assert "style=invis" in dot


# --- render_home picker/banner hooks ----------------------------------------

def test_home_has_local_view_containers(tmp_path):
    html = sv.render_home(_proj(tmp_path), "2", None).decode("utf-8")
    # the focus/expanded mounts the client fills are present (banner + bars)
    assert "id='rv-focus-banner'" in html
    assert "id='rv-expanded-bar'" in html
    assert "id='rv-graph'" in html


# --- /api/dot mirrors focus / anchor / too-large ----------------------------

class _FakeParsed:
    def __init__(self, query):
        self.query = query
        self.path = "/api/dot"


def _api_dot(proj, query):
    """Invoke the handler's _api_dot without a socket by capturing _json output."""
    Handler = sv.make_handler(proj)
    captured = {}

    class H(Handler):
        def __init__(self):  # bypass BaseHTTPRequestHandler.__init__
            pass

        def _json(self, code, obj):
            captured["code"] = code
            captured["obj"] = obj

    H()._api_dot(_FakeParsed(query))
    return captured["obj"]


def test_api_dot_mirrors_focus(tmp_path):
    proj = _proj(tmp_path, 8)
    out = _api_dot(proj, "tier=3&focus=s1")
    assert out["neighborhood"] is True
    assert out["focus"]["parent"] == "s1"
    assert out["too_large"] is None
    # matches render_home's focus dot
    home_dot = _dot(sv.render_home(proj, "3", "s1"))
    assert out["dot"] == home_dot


def test_api_dot_mirrors_anchor_clamp(tmp_path):
    proj = _proj(tmp_path, 8)
    out = _api_dot(proj, "tier=3&anchor=m3&radius=99")
    assert out["neighborhood"] is True
    assert out["anchor"] == {"id": "m3", "radius": 3}   # clamped
    home_dot = _dot(sv.render_home(proj, "3", None, "m3", "99"))
    assert out["dot"] == home_dot


def test_api_dot_mirrors_too_large_with_placeholder(tmp_path):
    n = sv.LARGE + 30
    proj = _proj(tmp_path, n)
    out = _api_dot(proj, "tier=3")
    assert out["too_large"] == {"tier": 3, "count": n, "threshold": sv.LARGE}
    assert out["nodes"] is not None and len(out["nodes"]) == n
    assert "__rv_placeholder__" in out["dot"]          # never the n-node graph
    assert out["dot"].count('" [') <= 1


def test_api_dot_flat_small_is_full(tmp_path):
    proj = _proj(tmp_path, 8)
    out = _api_dot(proj, "tier=3")
    assert out["too_large"] is None
    assert out["neighborhood"] is False
    assert "__rv_placeholder__" not in out["dot"]


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
