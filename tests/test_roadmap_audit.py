"""Tests for the roadmap-integrity layer.

Covers:
  * the STATUS_ALIASES contract between review_model and check_invariants
    (one vocabulary, two copies, asserted identical);
  * roadmap_audit's clauses over synthetic graph.json projects (status,
    grounding incl. the historical "exists" divergence, verified, content,
    provenance, slugs, targets, leanpaths);
  * run_audit / main() exit codes;
  * --enqueue mapping into task_queue.json (kinds, source, note, dedup,
    pseudo-node exclusion);
  * --stamp-verified against a fake Mathlib checkout (incl. MATHLIB_PATH);
  * merge_node's metadata.targets support.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "review_ui"))

import check_invariants as ci   # noqa: E402
import merge_node               # noqa: E402
import review_model as rm       # noqa: E402
import roadmap_audit as ra      # noqa: E402

STAMP = {"at": "2026-08-04T00:00:00Z", "method": "grep", "declarations": 1}


# ---------------------------------------------------------------------------
# synthetic project builder
# ---------------------------------------------------------------------------

def node(nid, tier=2, parent="c0", status="missing", deps=(), content="auto",
         refs="auto", **extra):
    rec = {"id": nid, "tier": tier, "parent": parent,
           "kind": "theorem" if tier == 2 else "section",
           "mathlib_status": status, "depends_on": list(deps)}
    if tier == 2:
        rec["content"] = f"informal_content/{nid}.md" if content == "auto" else content
        rec["source_refs"] = [{"kind": "textbook", "where": "ch. 1"}] if refs == "auto" else refs
    rec.update(extra)
    return rec


def cluster(nid="c0"):
    return node(nid, tier=1, parent=None, status="missing")


def make_project(tmp_path, nodes, metadata=None, missing_files=(), orphans=()):
    """Write a v2 graph.json + informal_content/ prose into tmp_path/proj."""
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    by_id = {rec["id"]: rec for rec in nodes}
    graph = {"version": 2, "metadata": metadata or {}, "nodes": by_id}
    gp = project / "graph.json"
    gp.write_text(json.dumps(graph, indent=2))
    (project / "informal_content").mkdir(exist_ok=True)
    for nid, rec in by_id.items():
        rel = rec.get("content")
        if rel and nid not in missing_files:
            path = project / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"Prose for {nid}.\n")
    for rel in orphans:
        path = project / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("orphaned prose\n")
    return gp


def healthy_project(tmp_path):
    """Every clause satisfied: canonical statuses, stamps, prose, refs, target."""
    return make_project(tmp_path, [
        cluster(),
        node("s1", status="in-mathlib", mathlib_declarations=["Nat.add_comm"],
             mathlib_verified=dict(STAMP)),
        node("s2", status="missing", deps=["s1"]),
    ], metadata={"targets": ["s2"]})


def offenders_of(report, clause):
    return {off["node"]: off["detail"] for off in report["clauses"][clause]}


# ---------------------------------------------------------------------------
# contract: the two status-alias tables must never drift
# ---------------------------------------------------------------------------

def test_status_alias_tables_are_identical():
    assert rm.STATUS_ALIASES == ci._STATUS_ALIASES


def test_normalizers_agree_on_every_alias_and_garbage():
    probes = list(rm.STATUS_ALIASES)
    probes += [k.upper() for k in rm.STATUS_ALIASES]
    probes += [f"  {k}  " for k in rm.STATUS_ALIASES]
    probes += [None, 42, 3.5, "", "banana", ["exists"], {"status": "exists"}, True]
    for probe in probes:
        assert rm.normalize_status(probe) == ci._normalize_status(probe), probe
    assert rm.normalize_status("exists") == "in-mathlib"
    assert rm.normalize_status(" Partially ") == "partial"
    assert rm.normalize_status(None) is None
    assert rm.normalize_status("banana") is None


# ---------------------------------------------------------------------------
# clause: status
# ---------------------------------------------------------------------------

def test_status_clause_flags_alias_and_garbage(tmp_path):
    gp = make_project(tmp_path, [
        cluster(),
        node("alias", status="exists"),
        node("junk", status="banana"),
        node("fine", status="missing", deps=["alias"]),
    ])
    report, _ = ra.run_audit(gp)
    offs = offenders_of(report, "status")
    assert "non-canonical spelling" in offs["alias"]
    assert "'in-mathlib'" in offs["alias"]
    assert "unrecognized mathlib_status" in offs["junk"]
    assert "fine" not in offs


# ---------------------------------------------------------------------------
# clause: grounding (normalized statuses — the historical "exists" divergence)
# ---------------------------------------------------------------------------

def test_grounding_clause_flags_unrooted_and_accepts_exists_alias(tmp_path):
    gp = make_project(tmp_path, [
        cluster(),
        node("g_ex", status="exists", mathlib_declarations=["Foo.bar"],
             mathlib_verified=dict(STAMP)),
        node("g_ok", status="missing", deps=["g_ex"]),
        node("g_bad", status="missing"),
    ])
    report, _ = ra.run_audit(gp)
    offs = offenders_of(report, "grounding")
    assert set(offs) == {"g_bad"}
    assert "no in-mathlib root" in offs["g_bad"]


# ---------------------------------------------------------------------------
# clause: verified
# ---------------------------------------------------------------------------

def test_verified_clause_without_verification(tmp_path):
    gp = make_project(tmp_path, [
        cluster(),
        node("v_nodecl", status="in-mathlib"),
        node("v_nostamp", status="in-mathlib", mathlib_declarations=["Foo.bar"]),
        node("v_stamped", status="in-mathlib", mathlib_declarations=["Foo.baz"],
             mathlib_verified=dict(STAMP)),
    ])
    report, stampable = ra.run_audit(gp)
    offs = offenders_of(report, "verified")
    assert "no mathlib_declarations" in offs["v_nodecl"]
    assert "never verified" in offs["v_nostamp"]
    assert "v_stamped" not in offs
    assert stampable == {}


# ---------------------------------------------------------------------------
# clause: content
# ---------------------------------------------------------------------------

def test_content_clause_null_missing_and_orphan(tmp_path):
    gp = make_project(tmp_path, [
        cluster(),
        node("n_null", content=None),
        node("n_gone"),
        node("n_ok"),
    ], missing_files={"n_gone"}, orphans=["informal_content/zzz_orphan.md"])
    report, _ = ra.run_audit(gp)
    offs = offenders_of(report, "content")
    assert "null content" in offs["n_null"]
    assert "missing or empty" in offs["n_gone"]
    assert "informal_content/zzz_orphan.md" in offs["(orphan)"]
    assert "n_ok" not in offs


# ---------------------------------------------------------------------------
# clause: provenance (tier-2 only — tier-1 clusters exempt)
# ---------------------------------------------------------------------------

def test_provenance_clause_tier2_only_and_in_mathlib_exempt(tmp_path):
    gp = make_project(tmp_path, [
        cluster(),                                    # tier-1: no refs, exempt
        node("p_bare", refs=None),
        node("p_refd"),
        node("p_blue", status="exists", refs=None,    # normalized in-mathlib: exempt
             mathlib_declarations=["Foo.bar"], mathlib_verified=dict(STAMP)),
    ])
    report, _ = ra.run_audit(gp)
    offs = offenders_of(report, "provenance")
    assert set(offs) == {"p_bare"}
    assert "no source_refs" in offs["p_bare"]


# ---------------------------------------------------------------------------
# clause: slugs
# ---------------------------------------------------------------------------

def test_slugs_clause_flags_two_ids_colliding_to_one_slug(tmp_path):
    # "foo bar" and "foo_bar" both slug to "foo_bar" — their prose paths collide.
    gp = make_project(tmp_path, [
        cluster(),
        node("foo bar"),
        node("foo_bar"),
    ])
    report, _ = ra.run_audit(gp)
    offs = report["clauses"]["slugs"]
    assert offs, "colliding ids 'foo bar' / 'foo_bar' must be flagged"
    assert any("foo bar" in off["node"] and "foo_bar" in off["node"] for off in offs)


# ---------------------------------------------------------------------------
# clause: targets
# ---------------------------------------------------------------------------

def test_targets_clause_unresolvable_tier1_and_ungrounded_cone(tmp_path):
    gp = make_project(tmp_path, [
        cluster(),
        node("t", status="missing", deps=["u"]),
        node("u", status="missing"),
    ], metadata={"targets": ["ghost", "c0", {"node": "t"}]})
    report, _ = ra.run_audit(gp)
    offs = report["clauses"]["targets"]
    assert any(o["node"] == "ghost" and "does not resolve" in o["detail"] for o in offs)
    assert any(o["node"] == "c0" and "tier-2" in o["detail"] for o in offs)
    cone_offs = {o["node"] for o in offs if "cone" in o["detail"]}
    assert {"t", "u"} <= cone_offs


def test_targets_clause_clean_on_grounded_cone(tmp_path):
    gp = healthy_project(tmp_path)
    report, _ = ra.run_audit(gp)
    assert report["clauses"]["targets"] == []
    assert "s2" in report["targets"]
    assert report["targets"]["s2"]["cone_size"] == 2


# ---------------------------------------------------------------------------
# clause: leanpaths
# ---------------------------------------------------------------------------

def test_leanpaths_clause_escape_absent_present(tmp_path):
    lean_root = tmp_path / "lean"
    lean_root.mkdir()
    (lean_root / "Present.lean").write_text("theorem present : True := trivial\n")
    gp = make_project(tmp_path, [
        cluster(),
        node("m", status="in-mathlib", mathlib_declarations=["Foo.bar"],
             mathlib_verified=dict(STAMP)),
        node("e_escape", deps=["m"], lean_file="../escape.lean"),
        node("e_absent", deps=["m"], lean_file="Absent.lean"),
        node("e_present", deps=["m"], lean_file="Present.lean"),
    ], metadata={"lean_root": str(lean_root)})
    report, _ = ra.run_audit(gp)
    offs = offenders_of(report, "leanpaths")
    assert set(offs) == {"e_escape", "e_absent"}
    assert "escapes" in offs["e_escape"]
    assert "absent" in offs["e_absent"]


# ---------------------------------------------------------------------------
# run_audit ok / exit codes
# ---------------------------------------------------------------------------

def test_run_audit_ok_on_healthy_project(tmp_path):
    gp = healthy_project(tmp_path)
    report, stampable = ra.run_audit(gp)
    assert report["ok"] is True
    assert all(offs == [] for offs in report["clauses"].values()), report["clauses"]
    assert stampable == {}


def test_main_exit_codes(tmp_path, capsys):
    healthy = healthy_project(tmp_path)
    assert ra.main([str(healthy)]) == 0
    bad_dir = tmp_path / "bad"
    bad_dir.mkdir()
    bad = make_project(bad_dir, [cluster(), node("lonely", status="missing")])
    assert ra.main([str(bad)]) == 1
    assert ra.main([str(tmp_path / "nope" / "graph.json")]) == 2
    capsys.readouterr()


# ---------------------------------------------------------------------------
# --enqueue
# ---------------------------------------------------------------------------

def _enqueue_project(tmp_path):
    lean_root = tmp_path / "lean"
    lean_root.mkdir()
    return make_project(tmp_path, [
        cluster(),
        # status offender + verified offender -> both mathcheck:a (must dedup)
        node("a", status="exists"),
        node("b", status="missing"),                 # grounding -> graphreview
        node("c", status="missing", refs=None),      # grounding + provenance
        node("m", status="in-mathlib", mathlib_declarations=["Foo.bar"],
             mathlib_verified=dict(STAMP)),
        node("d", deps=["m"], lean_file="../out.lean"),   # leanpaths -> escalation
    ], metadata={"lean_root": str(lean_root)},
        orphans=["informal_content/zzz_orphan.md"])       # content "(orphan)"


def test_enqueue_maps_kinds_dedups_and_skips_pseudo_nodes(tmp_path, capsys):
    gp = _enqueue_project(tmp_path)
    rc = ra.main([str(gp), "--enqueue"])
    capsys.readouterr()
    assert rc == 1
    queue = json.loads((gp.parent / "task_queue.json").read_text())
    keys = {(t["agent"], t["node"]) for t in queue}
    assert keys == {
        ("mathcheck", "a"),
        ("graphreview", "b"),
        ("graphreview", "c"),
        ("contentreview", "c"),
        ("escalation", "d"),
    }
    assert len(queue) == len(keys)          # (agent,node) dedup collapsed a's two offenders
    for task in queue:
        assert task["source"] == "engine"
        assert task["note"].startswith("audit:")
        assert task["status"] == "queued"
    assert not any(t["node"].startswith("(") for t in queue)   # (graph)/(orphan) never enqueued


def test_enqueue_is_idempotent_across_runs(tmp_path, capsys):
    gp = _enqueue_project(tmp_path)
    ra.main([str(gp), "--enqueue"])
    ra.main([str(gp), "--enqueue"])
    capsys.readouterr()
    queue = json.loads((gp.parent / "task_queue.json").read_text())
    assert len(queue) == 5


# ---------------------------------------------------------------------------
# --stamp-verified with a fake Mathlib checkout
# ---------------------------------------------------------------------------

def fake_mathlib(tmp_path):
    checkout = tmp_path / "mathlib_checkout"
    (checkout / "Mathlib").mkdir(parents=True)
    (checkout / "Mathlib" / "Basic.lean").write_text(
        "theorem Nat.add_comm2 (a b : Nat) : a + b = b + a := Nat.add_comm b a\n")
    return checkout


def _stamp_nodes():
    return [
        cluster(),
        node("ok", status="in-mathlib", mathlib_declarations=["Nat.add_comm2"]),
        node("bad", status="in-mathlib", mathlib_declarations=["Nat.zz_missing_decl"]),
    ]


def test_stamp_verified_stamps_resolving_and_flags_missing(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("LEAN_PLANNER_MATHLIB", raising=False)
    monkeypatch.delenv("MATHLIB_PATH", raising=False)
    checkout = fake_mathlib(tmp_path)
    gp = make_project(tmp_path, _stamp_nodes(), metadata={"lean_root": str(checkout)})
    rc = ra.main([str(gp), "--json", "--stamp-verified"])
    out = capsys.readouterr().out
    assert rc == 1                                   # "bad" is still an offender
    report = json.loads(out)
    assert report["stamped"] == 1
    offs = offenders_of(report, "verified")
    assert set(offs) == {"bad"}
    assert "not found in Mathlib" in offs["bad"]
    assert "Nat.zz_missing_decl" in offs["bad"]
    graph = json.loads(gp.read_text())               # stamp went through merge_node
    stamp = graph["nodes"]["ok"]["mathlib_verified"]
    assert stamp["method"] == "grep"
    assert stamp["declarations"] == 1
    assert "mathlib_verified" not in graph["nodes"]["bad"]


def test_stamp_verified_honors_mathlib_path_env(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("LEAN_PLANNER_MATHLIB", raising=False)
    checkout = fake_mathlib(tmp_path)
    monkeypatch.setenv("MATHLIB_PATH", str(checkout))
    plain_root = tmp_path / "leanproj"               # exists, but no Mathlib/ inside
    plain_root.mkdir()
    gp = make_project(tmp_path, [
        cluster(),
        node("ok", status="in-mathlib", mathlib_declarations=["Nat.add_comm2"]),
    ], metadata={"lean_root": str(plain_root)})
    rc = ra.main([str(gp), "--stamp-verified"])
    capsys.readouterr()
    assert rc == 0
    graph = json.loads(gp.read_text())
    assert graph["nodes"]["ok"]["mathlib_verified"]["method"] == "grep"


def test_stamp_rerun_is_clean_after_stamping(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("LEAN_PLANNER_MATHLIB", raising=False)
    monkeypatch.delenv("MATHLIB_PATH", raising=False)
    checkout = fake_mathlib(tmp_path)
    gp = make_project(tmp_path, [
        cluster(),
        node("ok", status="in-mathlib", mathlib_declarations=["Nat.add_comm2"]),
    ], metadata={"lean_root": str(checkout)})
    assert ra.main([str(gp), "--stamp-verified"]) == 0
    capsys.readouterr()
    report, stampable = ra.run_audit(gp)             # no verify: the stamp now suffices
    assert report["clauses"]["verified"] == []
    assert stampable == {}


# ---------------------------------------------------------------------------
# merge_node metadata.targets
# ---------------------------------------------------------------------------

def _merge_graph(tmp_path):
    return make_project(tmp_path, [
        cluster(),
        node("s1", status="in-mathlib", mathlib_declarations=["Foo.bar"],
             mathlib_verified=dict(STAMP)),
        node("s2", status="missing", deps=["s1"]),
    ])


def test_merge_persists_bare_and_dict_targets(tmp_path):
    gp = _merge_graph(tmp_path)
    result = merge_node.merge(str(gp), {"metadata": {"targets": ["s2", {"node": "s1"}]}})
    assert result["targets_set"] == 2
    graph = json.loads(gp.read_text())
    assert graph["metadata"]["targets"] == ["s2", {"node": "s1"}]


def test_merge_rejects_unknown_target_node(tmp_path):
    gp = _merge_graph(tmp_path)
    with pytest.raises(ValueError, match="does not resolve"):
        merge_node.merge(str(gp), {"metadata": {"targets": ["ghost"]}})
    assert "targets" not in json.loads(gp.read_text())["metadata"]


def test_merge_rejects_extra_metadata_keys(tmp_path):
    gp = _merge_graph(tmp_path)
    with pytest.raises(ValueError, match="may only set 'targets'"):
        merge_node.merge(str(gp), {"metadata": {"targets": ["s2"], "title": "nope"}})


def test_merge_rejects_non_list_targets(tmp_path):
    gp = _merge_graph(tmp_path)
    with pytest.raises(ValueError, match="must be a list"):
        merge_node.merge(str(gp), {"metadata": {"targets": "s2"}})


def test_targets_survive_later_plain_upsert(tmp_path):
    gp = _merge_graph(tmp_path)
    merge_node.merge(str(gp), {"metadata": {"targets": ["s2"]}})
    merge_node.merge(str(gp), {"upsert": {"s3": node("s3", deps=["s1"])}})
    graph = json.loads(gp.read_text())
    assert graph["metadata"]["targets"] == ["s2"]
    assert "s3" in graph["nodes"]
