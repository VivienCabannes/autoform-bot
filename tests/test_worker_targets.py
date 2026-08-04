"""Target/critical-path layer tests — review_model target functions + survey ordering.

Pure-model half: graph_targets / dependency_cone / untrusted_depth / target_metrics /
prove_priority over synthetic tmp_path graphs. Survey half: collect() orders the prove
bucket by mission priority (in-cone first, tallest untrusted chain above first) with a
canned gh runner and no claim board. Everything is offline.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))
sys.path.insert(0, str(_ROOT / "scripts" / "review_ui"))

import review_model as rm  # noqa: E402

from autoform_worker import survey  # noqa: E402
from autoform_worker.config import resolve_config  # noqa: E402
from autoform_worker.counters import Counters  # noqa: E402
from autoform_worker.githost import GitHost  # noqa: E402

METRIC_KEYS = {"cone_size", "unproved_mass", "ready", "critical_path", "done"}


# -- fixtures-by-hand --------------------------------------------------------

def _node(tier=2, deps=(), status="missing", parent=None, kind="theorem"):
    return {"tier": tier, "parent": parent, "kind": kind, "depends_on": list(deps),
            "mathlib_status": status, "content": "stmt", "source_refs": []}


def chain_graph(tmp_path, extra=None):
    """A (in-mathlib, trusted) <- B <- C <- D, all tier 2, loaded via the real loader."""
    nodes = {
        "A": _node(status="in-mathlib"),
        "B": _node(deps=["A", "ghost-dangling"]),
        "C": _node(deps=["B"]),
        "D": _node(deps=["C"]),
    }
    nodes.update(extra or {})
    gp = tmp_path / "graph.json"
    gp.write_text(json.dumps({"version": 2, "metadata": {"lean_root": str(tmp_path)},
                              "nodes": nodes}), encoding="utf-8")
    return rm.load_graph(gp)[0]


def sidecar_with(clean=()):
    sc = rm.empty_sidecar()
    for nid in clean:
        sc["reviews"][nid] = {"ai": {"verdict": "clean"}}
    return sc


# -- graph_targets -----------------------------------------------------------

def test_graph_targets_bare_ids_and_records():
    meta = {"targets": ["D", {"node": "E", "why": "mission"}, "F"]}
    assert rm.graph_targets(meta) == ["D", "E", "F"]


def test_graph_targets_ignores_junk_entries():
    meta = {"targets": ["", 7, None, {"why": "no node key"}, {"node": 3}, {"node": ""},
                        ["nested"], "ok"]}
    assert rm.graph_targets(meta) == ["ok"]


def test_graph_targets_absent_or_empty():
    assert rm.graph_targets({}) == []
    assert rm.graph_targets({"targets": []}) == []
    assert rm.graph_targets({"targets": None}) == []
    assert rm.graph_targets(None) == []


# -- dependency_cone ---------------------------------------------------------

def test_dependency_cone_is_target_plus_transitive_deps_only(tmp_path):
    # X depends ON D: a dependent, so it must never enter D's cone.
    nodes = chain_graph(tmp_path, extra={"X": _node(deps=["D"])})
    assert rm.dependency_cone("D", nodes) == {"A", "B", "C", "D"}
    assert rm.dependency_cone("C", nodes) == {"A", "B", "C"}
    assert rm.dependency_cone("A", nodes) == {"A"}
    # dangling dep ids (not real nodes) are excluded by the closure
    assert "ghost-dangling" not in rm.dependency_cone("D", nodes)


def test_dependency_cone_unknown_target_is_empty(tmp_path):
    nodes = chain_graph(tmp_path)
    assert rm.dependency_cone("nope", nodes) == set()


# -- untrusted_depth ---------------------------------------------------------

def test_untrusted_depth_chain(tmp_path):
    nodes = chain_graph(tmp_path)
    depths = rm.untrusted_depth(nodes, sidecar_with())
    assert depths == {"B": 1, "C": 2, "D": 3}
    assert "A" not in depths  # a trusted node never appears in the result


def test_untrusted_depth_shifts_when_sidecar_marks_b_clean(tmp_path):
    nodes = chain_graph(tmp_path)
    depths = rm.untrusted_depth(nodes, sidecar_with(clean=["B"]))
    assert depths == {"C": 1, "D": 2}


def test_untrusted_depth_within_scoping_excludes_out_of_scope_deps(tmp_path):
    nodes = chain_graph(tmp_path)
    assert rm.untrusted_depth(nodes, sidecar_with(), within={"C", "D"}) == {"C": 1, "D": 2}
    assert rm.untrusted_depth(nodes, sidecar_with(), within={"D"}) == {"D": 1}
    assert rm.untrusted_depth(nodes, sidecar_with(), within=set()) == {}


def test_untrusted_depth_all_values_positive(tmp_path):
    nodes = chain_graph(tmp_path)
    depths = rm.untrusted_depth(nodes, sidecar_with(clean=["B", "C", "D"]))
    assert depths == {}  # every node trusted -> nothing untrusted anywhere


# -- target_metrics ----------------------------------------------------------

def test_target_metrics_on_chain(tmp_path):
    nodes = chain_graph(tmp_path)
    m = rm.target_metrics("D", nodes, sidecar_with())
    assert set(m) == METRIC_KEYS
    assert m["cone_size"] == 4
    assert m["unproved_mass"] == 3
    assert m["ready"] == 1          # only B: its sole real dep A is trusted
    assert m["critical_path"] == 3
    assert m["done"] is False


def test_target_metrics_done_when_whole_cone_trusted(tmp_path):
    nodes = chain_graph(tmp_path)
    m = rm.target_metrics("D", nodes, sidecar_with(clean=["B", "C", "D"]))
    assert m == {"cone_size": 4, "unproved_mass": 0, "ready": 0,
                 "critical_path": 0, "done": True}


def test_target_metrics_unknown_target(tmp_path):
    nodes = chain_graph(tmp_path)
    m = rm.target_metrics("nope", nodes, sidecar_with())
    assert m == {"cone_size": 0, "unproved_mass": 0, "ready": 0,
                 "critical_path": 0, "done": False}


# -- prove_priority ----------------------------------------------------------

def test_prove_priority_empty_without_targets(tmp_path):
    nodes = chain_graph(tmp_path)
    assert rm.prove_priority(nodes, sidecar_with(), {}) == {}
    assert rm.prove_priority(nodes, sidecar_with(), {"targets": []}) == {}


def test_prove_priority_cone_before_offcone_and_tall_chain_first(tmp_path):
    # D (the target) also depends on sibling leaf E; F sits entirely off-cone.
    extra = {"E": _node(), "F": _node()}
    nodes = chain_graph(tmp_path, extra=extra)
    nodes["D"]["depends_on"] = ["C", "E"]
    prio = rm.prove_priority(nodes, sidecar_with(), {"targets": ["D"]})

    assert set(prio) == set(nodes)  # a key for every node
    # in-cone nodes carry bucket 0, off-cone bucket 1
    for nid in ("A", "B", "C", "D", "E"):
        assert prio[nid][0] == 0
    assert prio["F"][0] == 1
    # among the ready in-cone leaves, B (3-chain B<-C<-D above it... 2 untrusted
    # links) beats E (only D above it): tallest untrusted chain above sorts first.
    assert prio["B"] < prio["E"] < prio["F"]  # tuples are lower-first
    assert prio["B"][1] == -2 and prio["E"][1] == -1
    ordered = sorted(("F", "E", "B"), key=prio.get)
    assert ordered == ["B", "E", "F"]


def test_prove_priority_ties_break_stably_by_id(tmp_path):
    nodes = chain_graph(tmp_path, extra={"F": _node(), "G": _node()})
    prio = rm.prove_priority(nodes, sidecar_with(), {"targets": ["D"]})
    assert prio["F"] == (1, 0, "F") and prio["G"] == (1, 0, "G")
    assert prio["F"] < prio["G"]


# -- survey.collect(): target-aware prove ordering ---------------------------

def make_target_cfg(tmp_path, monkeypatch, worker_id="wa", targets=("zz-goal",)):
    """A dispatch project whose graph declares mission targets.

    Prove-eligible nodes: zz-mid (on zz-goal's critical path) and aa-off (off-cone).
    Alphabetical order would put aa-off first — the target sort must not.
    """
    lean = tmp_path / "lean"
    lean.mkdir(exist_ok=True)
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    nodes = {
        "t1": _node(tier=1, status="in-mathlib", kind="section"),
        "zz-mid": _node(deps=["t1"], parent="t1"),
        "zz-goal": _node(deps=["zz-mid"], parent="t1"),
        "aa-off": _node(deps=["t1"], parent="t1"),
    }
    meta = {"lean_root": str(lean)}
    if targets:
        meta["targets"] = list(targets)
    (proj / "graph.json").write_text(
        json.dumps({"version": 2, "metadata": meta, "nodes": nodes}), encoding="utf-8")
    monkeypatch.setenv("AUTOFORM_DISPATCH_PROJECT", str(proj))
    monkeypatch.setenv("AUTOFORM_WORKER_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("AUTOFORM_CONFIG", str(tmp_path / "autoform-config.json"))
    monkeypatch.setenv("AUTOFORM_GIT_BASE_URL", str(tmp_path / "remotes"))
    monkeypatch.delenv("AUTOFORM_RESPECT_CLAIMS", raising=False)
    monkeypatch.delenv("AUTOFORM_CANONICAL_REPO", raising=False)
    return resolve_config(worker_id=worker_id)


def make_runner(login="me", permission="WRITE"):
    """A tiny canned gh (no PRs, no issues) — same routing as test_worker_survey."""
    repo = {"nameWithOwner": "o/r", "defaultBranchRef": {"name": "main"}, "isFork": False,
            "parent": None, "hasIssuesEnabled": True, "viewerPermission": permission,
            "visibility": "PRIVATE"}

    def done(payload):
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=payload, stderr="")

    def fake(args, input_text=None):
        if args[:2] == ["api", "user"]:
            return done(login + "\n")
        if args[:2] == ["repo", "view"]:
            return done(json.dumps(repo))
        if args[:2] == ["pr", "list"]:
            return done(json.dumps([]))
        if args[:2] == ["issue", "list"]:
            return done(json.dumps([]))
        raise AssertionError(f"unexpected gh argv: {args}")

    return fake


def run_collect(cfg):
    return survey.collect(cfg, GitHost(runner=make_runner()), None,
                          Counters(cfg.counters_path), "o/r", "main")


def prove_nodes(s):
    return [c.node for c in s.actionable("prove")]


def test_collect_targets_put_on_cone_node_first(tmp_path, monkeypatch):
    # "wa" and "wb" shuffle a 2-element list in OPPOSITE orders (checked below via
    # the no-targets test); the stable priority sort must beat both the shuffle and
    # the alphabetical order (which would pick aa-off first).
    for wid in ("wa", "wb"):
        cfg = make_target_cfg(tmp_path, monkeypatch, worker_id=wid)
        s = run_collect(cfg)
        assert prove_nodes(s) == ["zz-mid", "aa-off"], f"worker {wid}"


def test_collect_targets_metrics_in_to_json(tmp_path, monkeypatch):
    cfg = make_target_cfg(tmp_path, monkeypatch)
    s = run_collect(cfg)
    assert set(s.targets) == {"zz-goal"}
    assert set(s.targets["zz-goal"]) == METRIC_KEYS
    payload = s.to_json()["targets"]["zz-goal"]
    assert payload["cone_size"] == 3          # zz-goal + zz-mid + t1
    assert payload["unproved_mass"] == 2      # zz-mid, zz-goal
    assert payload["ready"] == 1              # zz-mid (t1 trusted)
    assert payload["critical_path"] == 2
    assert payload["done"] is False


def test_collect_targets_unknown_target_id_is_dropped(tmp_path, monkeypatch):
    cfg = make_target_cfg(tmp_path, monkeypatch, targets=("zz-goal", "ghost"))
    s = run_collect(cfg)
    assert set(s.targets) == {"zz-goal"}  # only targets that exist in the graph


def test_collect_no_targets_falls_back_to_worker_shuffle(tmp_path, monkeypatch):
    orders = set()
    for wid in ("w0", "w1", "w2", "w3", "w4", "w5"):
        cfg = make_target_cfg(tmp_path, monkeypatch, worker_id=wid, targets=())
        s = run_collect(cfg)
        got = prove_nodes(s)
        assert sorted(got) == ["aa-off", "zz-mid"]  # every eligible node present
        assert s.targets == {} and s.to_json()["targets"] == {}
        orders.add(tuple(got))
    # seeded shuffle really decides: both orders occur across worker ids
    assert orders == {("zz-mid", "aa-off"), ("aa-off", "zz-mid")}
