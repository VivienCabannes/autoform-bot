"""Tests for the drag-and-drop agent dispatch surface (serve_review).

Covers the write-only dispatch queue + the dispatch API:

  * ``Project.task_queue`` — read ``task_queue.json`` next to graph.json; absent /
    corrupt / mis-shaped → ``[]``; never raises.
  * ``Project.write_task_queue`` — atomic (temp + ``os.replace``), capped at 200.
  * ``GET  /api/dispatch``        — ``{palette, queue, live, backend}`` (palette of 8,
    the existing agents feed under ``live``).
  * ``POST /api/request``         — enqueue + validate (unknown agent/node → 400) +
    DEDUPE (identical agent+node queued/running never duplicates) + the exact task
    record shape.
  * ``POST /api/request/cancel``  — remove a *queued* task only (never a running one).

The HTTP cases run against a real ``ThreadingHTTPServer`` on an ephemeral port so
the actual ``do_GET`` / ``do_POST`` routing + status codes are exercised, and
``/api/agents`` is checked to still work unchanged.
"""
import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
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
    ],
}


def _proj(tmp_path):
    gp = tmp_path / "graph.json"
    gp.write_text(json.dumps(GRAPH))
    return sv.Project(gp)


# ---------------------------------------------------------------------------
# Project.task_queue — read, never raises
# ---------------------------------------------------------------------------

def test_task_queue_absent_is_empty(tmp_path):
    proj = _proj(tmp_path)
    assert not proj.task_queue_path.exists()
    assert proj.task_queue() == []


def test_task_queue_corrupt_is_empty(tmp_path):
    proj = _proj(tmp_path)
    proj.task_queue_path.write_text("{not json")
    assert proj.task_queue() == []


def test_task_queue_non_list_root_is_empty(tmp_path):
    proj = _proj(tmp_path)
    proj.task_queue_path.write_text(json.dumps({"oops": 1}))
    assert proj.task_queue() == []


def test_task_queue_drops_non_dict_entries(tmp_path):
    proj = _proj(tmp_path)
    proj.task_queue_path.write_text(json.dumps(
        [{"id": "worker:s1", "status": "queued"}, "bad", 7]))
    q = proj.task_queue()
    assert len(q) == 1
    assert q[0]["id"] == "worker:s1"


# ---------------------------------------------------------------------------
# Project.write_task_queue — atomic, bounded, write-only
# ---------------------------------------------------------------------------

def test_write_task_queue_roundtrips(tmp_path):
    proj = _proj(tmp_path)
    rec = {"id": "reviewer:s1", "agent": "reviewer", "node": "s1",
           "status": "queued"}
    proj.write_task_queue([rec])
    assert proj.task_queue() == [rec]
    # No stray temp file left behind by the atomic write.
    assert not proj.task_queue_path.with_name(
        proj.task_queue_path.name + ".tmp").exists()


def test_write_task_queue_caps_at_200(tmp_path):
    proj = _proj(tmp_path)
    big = [{"id": f"t{i}", "status": "queued"} for i in range(250)]
    proj.write_task_queue(big)
    saved = proj.task_queue()
    assert len(saved) == sv.TASK_QUEUE_CAP == 200
    # the cap keeps the MOST RECENT entries (the tail)
    assert saved[0]["id"] == "t50"
    assert saved[-1]["id"] == "t249"


def test_write_task_queue_does_not_touch_graph_or_sidecar(tmp_path):
    proj = _proj(tmp_path)
    graph_before = proj.graph_path.read_text()
    proj.write_task_queue([{"id": "worker:s1", "status": "queued"}])
    # the dispatch write only ever creates task_queue.json
    assert proj.graph_path.read_text() == graph_before
    assert not proj.sidecar_path.exists()
    assert proj.task_queue_path.exists()


# ---------------------------------------------------------------------------
# palette constant
# ---------------------------------------------------------------------------

_ALL_KINDS = {"reviewer", "worker", "planner", "graphreview",
              "contentreview", "holistic", "mathcheck", "escalation"}


def test_palette_covers_every_dispatch_kind():
    ids = [a["id"] for a in sv.AGENT_PALETTE]
    assert set(ids) == _ALL_KINDS
    assert len(ids) == len(set(ids))                      # no duplicates
    for a in sv.AGENT_PALETTE:
        assert set(a) >= {"id", "label", "icon", "blurb", "applies"}


def test_palette_partitions_into_engine_and_orchestrator_kinds():
    # The dispatch_queue lifecycle constants must EXACTLY partition the palette: the
    # engine drains the engine kinds; the orchestrator must claim→run→done all the
    # rest. A palette kind in NEITHER set would silently dangle (never resolved) — the
    # exact bug this guards against, so the partition can't drift as kinds are added.
    import dispatch_queue as dq
    palette = {a["id"] for a in sv.AGENT_PALETTE}
    assert dq._ENGINE_KINDS == ("reviewer", "worker")
    assert not (set(dq._ENGINE_KINDS) & set(dq._ORCH_KINDS))        # disjoint
    assert set(dq._ENGINE_KINDS) | set(dq._ORCH_KINDS) == palette   # exhaustive


# ---------------------------------------------------------------------------
# live HTTP server fixture — exercises do_GET / do_POST routing + codes
# ---------------------------------------------------------------------------

class _Server:
    def __init__(self, proj):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), sv.make_handler(proj))
        self.port = self.httpd.server_address[1]
        self.t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.t.start()

    def _url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def get(self, path):
        with urllib.request.urlopen(self._url(path)) as r:
            return r.status, json.loads(r.read())

    def post(self, path, body):
        data = json.dumps(body).encode()
        req = urllib.request.Request(self._url(path), data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def _serve(tmp_path):
    return _Server(_proj(tmp_path))


# ---------------------------------------------------------------------------
# GET /api/dispatch shape — palette + queue + live (existing agents payload)
# ---------------------------------------------------------------------------

def test_api_dispatch_shape(tmp_path):
    srv = _serve(tmp_path)
    try:
        code, body = srv.get("/api/dispatch")
        assert code == 200
        assert set(body) == {"palette", "queue", "live", "backend"}
        assert {a["id"] for a in body["palette"]} == _ALL_KINDS
        assert body["queue"] == []
        # live is the existing agents feed payload (idle when nothing runs)
        assert body["live"]["orchestrator"]["state"] == "idle"
        assert body["live"]["agents"] == []
    finally:
        srv.close()


def test_api_agents_still_works(tmp_path):
    # /api/dispatch must be a superset; /api/agents stays available unchanged
    srv = _serve(tmp_path)
    try:
        code, body = srv.get("/api/agents")
        assert code == 200
        assert body == {"orchestrator": {"state": "idle"}, "agents": []}
    finally:
        srv.close()


# ---------------------------------------------------------------------------
# POST /api/request — enqueue + the exact task record shape
# ---------------------------------------------------------------------------

def test_request_enqueues_with_record_shape(tmp_path):
    srv = _serve(tmp_path)
    try:
        code, body = srv.post("/api/request", {"agent": "reviewer", "node": "s1"})
        assert code == 200
        assert body["ok"] is True
        assert len(body["queue"]) == 1
        rec = body["queue"][0]
        assert rec["id"] == "reviewer:s1"
        assert rec["agent"] == "reviewer"
        assert rec["node"] == "s1"
        assert rec["node_label"] == "Stmt 1"          # node name, not id
        assert rec["status"] == "queued"
        assert rec["requested_by"] == "dashboard"
        assert rec["at"].endswith("Z")                # iso UTC
        # and it actually landed on disk
        assert srv.get("/api/dispatch")[1]["queue"][0]["id"] == "reviewer:s1"
    finally:
        srv.close()


def test_node_label_falls_back_to_id(tmp_path):
    # a node without a name labels by its id
    g = {"metadata": {}, "nodes": [{"id": "nameless", "tier": 2, "parent": None,
                                    "kind": "lemma", "mathlib_status": "missing"}]}
    (tmp_path / "graph.json").write_text(json.dumps(g))
    srv = _Server(sv.Project(tmp_path / "graph.json"))
    try:
        code, body = srv.post("/api/request",
                              {"agent": "worker", "node": "nameless"})
        assert code == 200
        assert body["queue"][0]["node_label"] == "nameless"
    finally:
        srv.close()


# ---------------------------------------------------------------------------
# POST /api/request — validation (unknown agent / node → 400)
# ---------------------------------------------------------------------------

def test_request_unknown_agent_is_400(tmp_path):
    srv = _serve(tmp_path)
    try:
        code, body = srv.post("/api/request", {"agent": "ghost", "node": "s1"})
        assert code == 400
        assert body["ok"] is False
        # nothing was written
        assert srv.get("/api/dispatch")[1]["queue"] == []
    finally:
        srv.close()


def test_request_unknown_node_is_400(tmp_path):
    srv = _serve(tmp_path)
    try:
        code, body = srv.post("/api/request", {"agent": "reviewer", "node": "nope"})
        assert code == 400
        assert body["ok"] is False
        assert srv.get("/api/dispatch")[1]["queue"] == []
    finally:
        srv.close()


def test_request_missing_fields_is_400(tmp_path):
    srv = _serve(tmp_path)
    try:
        assert srv.post("/api/request", {})[0] == 400
        assert srv.post("/api/request", {"agent": "reviewer"})[0] == 400
        assert srv.post("/api/request", {"node": "s1"})[0] == 400
    finally:
        srv.close()


# ---------------------------------------------------------------------------
# POST /api/request — DEDUPE (identical agent+node queued/running)
# ---------------------------------------------------------------------------

def test_request_dedupes_identical(tmp_path):
    srv = _serve(tmp_path)
    try:
        srv.post("/api/request", {"agent": "reviewer", "node": "s1"})
        code, body = srv.post("/api/request", {"agent": "reviewer", "node": "s1"})
        assert code == 200
        # a second identical POST does NOT add a duplicate
        assert len(body["queue"]) == 1
        # a DIFFERENT agent on the same node IS a distinct task
        code, body = srv.post("/api/request", {"agent": "worker", "node": "s1"})
        assert len(body["queue"]) == 2
    finally:
        srv.close()


def test_request_dedupes_against_running(tmp_path):
    # a task the orchestrator marked "running" still blocks a re-enqueue
    proj = _proj(tmp_path)
    proj.write_task_queue([{"id": "reviewer:s1", "agent": "reviewer", "node": "s1",
                            "status": "running"}])
    srv = _Server(proj)
    try:
        code, body = srv.post("/api/request", {"agent": "reviewer", "node": "s1"})
        assert code == 200
        assert len(body["queue"]) == 1
        assert body["queue"][0]["status"] == "running"   # not faked back to queued
    finally:
        srv.close()


# ---------------------------------------------------------------------------
# POST /api/request/cancel — remove a queued task only
# ---------------------------------------------------------------------------

def test_cancel_removes_queued(tmp_path):
    srv = _serve(tmp_path)
    try:
        srv.post("/api/request", {"agent": "reviewer", "node": "s1"})
        code, body = srv.post("/api/request/cancel", {"id": "reviewer:s1"})
        assert code == 200
        assert body["ok"] is True
        assert body["queue"] == []
        assert srv.get("/api/dispatch")[1]["queue"] == []
    finally:
        srv.close()


def test_cancel_never_removes_running(tmp_path):
    proj = _proj(tmp_path)
    proj.write_task_queue([{"id": "reviewer:s1", "agent": "reviewer", "node": "s1",
                            "status": "running"}])
    srv = _Server(proj)
    try:
        code, body = srv.post("/api/request/cancel", {"id": "reviewer:s1"})
        assert code == 200
        # a running task is never cancellable from the dashboard
        assert len(body["queue"]) == 1
        assert body["queue"][0]["status"] == "running"
    finally:
        srv.close()


def test_cancel_unknown_id_is_noop(tmp_path):
    srv = _serve(tmp_path)
    try:
        srv.post("/api/request", {"agent": "reviewer", "node": "s1"})
        code, body = srv.post("/api/request/cancel", {"id": "nope:nope"})
        assert code == 200
        assert len(body["queue"]) == 1
    finally:
        srv.close()


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
