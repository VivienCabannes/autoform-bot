"""Tests for the live agent activity feed loader (``review_model.load_agents``).

It must never raise: absent / unreadable / corrupt / mis-shaped inputs all degrade
to ``{"orchestrator": {"state": "idle"}, "agents": []}``; a well-formed feed is
returned normalized (orchestrator dict with a state, agents list of dicts).
"""
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "scripts" / "review_ui"))
sys.path.insert(0, str(_HERE.parent / "scripts"))

import review_model as rm        # noqa: E402


def test_absent_file_is_idle(tmp_path):
    feed = rm.load_agents(tmp_path / "nope.json")
    assert feed == {"orchestrator": {"state": "idle"}, "agents": []}


def test_corrupt_json_is_idle(tmp_path):
    p = tmp_path / "agents_status.json"
    p.write_text("{not valid json")
    feed = rm.load_agents(p)
    assert feed["orchestrator"]["state"] == "idle"
    assert feed["agents"] == []


def test_non_object_root_is_idle(tmp_path):
    p = tmp_path / "agents_status.json"
    p.write_text("[1, 2, 3]")
    assert rm.load_agents(p) == {"orchestrator": {"state": "idle"}, "agents": []}


def test_missing_orchestrator_defaults_to_idle(tmp_path):
    p = tmp_path / "agents_status.json"
    p.write_text(json.dumps({"agents": []}))
    feed = rm.load_agents(p)
    assert feed["orchestrator"]["state"] == "idle"


def test_orchestrator_without_state_gets_idle(tmp_path):
    p = tmp_path / "agents_status.json"
    p.write_text(json.dumps({"orchestrator": {"phase": "x"}, "agents": []}))
    feed = rm.load_agents(p)
    assert feed["orchestrator"]["state"] == "idle"
    assert feed["orchestrator"]["phase"] == "x"


def test_agents_non_list_becomes_empty(tmp_path):
    p = tmp_path / "agents_status.json"
    p.write_text(json.dumps({"orchestrator": {"state": "running"},
                             "agents": "oops"}))
    feed = rm.load_agents(p)
    assert feed["agents"] == []
    assert feed["orchestrator"]["state"] == "running"


def test_non_dict_agent_entries_dropped(tmp_path):
    p = tmp_path / "agents_status.json"
    p.write_text(json.dumps({
        "orchestrator": {"state": "running"},
        "agents": [{"role": "worker", "name": "w", "target": "n"}, "bad", 7],
    }))
    feed = rm.load_agents(p)
    assert len(feed["agents"]) == 1
    assert feed["agents"][0]["name"] == "w"


def test_wellformed_feed_passes_through(tmp_path):
    payload = {
        "updated_at": "2026-06-19T12:00:00Z",
        "orchestrator": {"state": "running", "phase": "Phase 3 — prove",
                         "detail": "2 agents"},
        "agents": [
            {"role": "worker", "name": "autoform-worker",
             "target": "encoding_lemma", "state": "proving",
             "since": "2026-06-19T11:59:00Z"},
            {"role": "reviewer", "name": "proof-integrity-reviewer",
             "target": "reduction_lemma", "state": "judging",
             "since": "2026-06-19T11:58:00Z"},
        ],
    }
    p = tmp_path / "agents_status.json"
    p.write_text(json.dumps(payload))
    feed = rm.load_agents(p)
    assert feed["updated_at"] == "2026-06-19T12:00:00Z"
    assert feed["orchestrator"]["phase"] == "Phase 3 — prove"
    assert [a["role"] for a in feed["agents"]] == ["worker", "reviewer"]
    assert feed["agents"][0]["target"] == "encoding_lemma"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
