import json
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import recovery_state as rs  # noqa: E402


def project(tmp_path):
    (tmp_path / "informal_content").mkdir()
    (tmp_path / "informal_content" / "target.md").write_text("route one", encoding="utf-8")
    (tmp_path / "Target.lean").write_text("theorem target : True := by sorry", encoding="utf-8")
    graph = {
        "nodes": {
            "target": {
                "id": "target",
                "content": "informal_content/target.md",
                "lean_file": "Target.lean",
            }
        }
    }
    graph_path = tmp_path / "graph.json"
    graph_path.write_text(json.dumps(graph), encoding="utf-8")
    return graph_path


def test_fingerprint_changes_only_for_material_prover_inputs(tmp_path):
    graph_path = project(tmp_path)
    first = rs.proof_fingerprint(graph_path, "target", tmp_path, "claude")
    assert first == rs.proof_fingerprint(graph_path, "target", tmp_path, "claude")

    (tmp_path / "unrelated.log").write_text("noise", encoding="utf-8")
    assert first == rs.proof_fingerprint(graph_path, "target", tmp_path, "claude")

    (tmp_path / "informal_content" / "target.md").write_text("route two", encoding="utf-8")
    assert first != rs.proof_fingerprint(graph_path, "target", tmp_path, "claude")
    assert first != rs.proof_fingerprint(graph_path, "target", tmp_path, "codex")


def test_unchanged_recovery_requires_a_completed_or_parked_record(tmp_path):
    graph_path = project(tmp_path)
    fingerprint = rs.proof_fingerprint(graph_path, "target", tmp_path, "claude")
    task = {
        "agent": "escalation",
        "node": "target",
        "status": "done",
        "recovery": {"fingerprint": fingerprint},
    }
    assert rs.unchanged_recovery([task], "target", graph_path, tmp_path, "claude")
    task["status"] = "queued"
    assert not rs.unchanged_recovery([task], "target", graph_path, tmp_path, "claude")
