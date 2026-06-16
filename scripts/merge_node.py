#!/usr/bin/env python3
"""Single-writer incremental merge into a formalization plan's graph.json.

This is the one writer of graph.json. Splitters return their structural node
records and write only their own content files; the orchestrator routes every
structural change through here. An exclusive file lock serializes concurrent
callers, and the file is replaced atomically, so concurrent splitters never race
and a crash mid-write cannot corrupt the file.

Payload (JSON, from --payload FILE or stdin):

    {
      "upsert": {"<id>": {<node record>}, ...},   # or a list of node records
      "delete": ["<id>", ...]                      # optional
    }

A node record is a structural node object as described in
skills/plan/references/plan-json-schema.md. An upserted record's "id" must match
its key (it is filled in from the key when omitted). Deleting a node also strips
it from every other node's "depends_on", leaving the file self-consistent;
stripped edges are reported so the orchestrator can see them.

Usage:
    merge_node.py <graph.json> [--payload payload.json]
    splitter | merge_node.py <graph.json>          # payload on stdin
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

try:
    import fcntl  # POSIX advisory file locking
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _load_payload(path: str | None) -> dict:
    raw = open(path, encoding="utf-8").read() if path else sys.stdin.read()
    if not raw.strip():
        return {}
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object with 'upsert' and/or 'delete'")
    return payload


def _normalize_upsert(upsert) -> dict:
    """Accept either {id: record} or [record, ...]; return {id: record}."""
    if upsert is None:
        return {}
    if isinstance(upsert, list):
        out: dict = {}
        for rec in upsert:
            nid = rec.get("id")
            if not nid:
                raise ValueError(f"upsert record missing 'id': {rec!r}")
            out[nid] = rec
        return out
    if isinstance(upsert, dict):
        for key, rec in upsert.items():
            rid = rec.get("id")
            if rid is not None and rid != key:
                raise ValueError(f"upsert key {key!r} does not match record id {rid!r}")
            rec.setdefault("id", key)
        return upsert
    raise ValueError("'upsert' must be an object or a list")


def _atomic_write(path: str, data: dict) -> None:
    """Write to a temp file in the same directory, then rename over the target."""
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".graph.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)  # atomic on POSIX
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def merge(graph_path: str, payload: dict) -> dict:
    """Apply upserts and deletes to graph.json. Caller holds the lock."""
    upserts = _normalize_upsert(payload.get("upsert"))
    deletes = list(payload.get("delete", []))

    with open(graph_path, encoding="utf-8") as f:
        graph = json.load(f)
    nodes = graph.setdefault("nodes", {})

    for nid in deletes:
        nodes.pop(nid, None)
    for nid, rec in upserts.items():
        nodes[nid] = rec

    # Keep depends_on self-consistent: drop references to nodes no longer present.
    stripped = []
    for nid, rec in nodes.items():
        deps = rec.get("depends_on")
        if not deps:
            continue
        kept = [d for d in deps if d in nodes]
        if len(kept) != len(deps):
            stripped.extend((nid, d) for d in deps if d not in nodes)
            rec["depends_on"] = kept

    graph.setdefault("metadata", {})["last_updated"] = _now()

    _atomic_write(graph_path, graph)
    return {
        "upserted": len(upserts),
        "deleted": len(deletes),
        "stripped_edges": stripped,
        "total_nodes": len(nodes),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Single-writer incremental merge into a plan's graph.json"
    )
    ap.add_argument("graph", help="path to graph.json")
    ap.add_argument("--payload", help="path to a JSON payload file (default: read stdin)")
    args = ap.parse_args(argv)

    if not os.path.exists(args.graph):
        print(f"error: {args.graph} does not exist", file=sys.stderr)
        return 2

    payload = _load_payload(args.payload)
    if not payload.get("upsert") and not payload.get("delete"):
        print("nothing to merge (empty payload)", file=sys.stderr)
        return 0

    # Serialize concurrent callers on a sidecar lock file (the graph itself is
    # replaced atomically, so the lock lives on a stable inode beside it).
    lock_path = args.graph + ".lock"
    with open(lock_path, "w") as lock:
        if fcntl is not None:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            result = merge(args.graph, payload)
        finally:
            if fcntl is not None:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    msg = (
        f"merged: +{result['upserted']} upsert, -{result['deleted']} delete, "
        f"{result['total_nodes']} nodes total"
    )
    if result["stripped_edges"]:
        edges = ", ".join(f"{a} -> {b}" for a, b in result["stripped_edges"])
        msg += f"; stripped {len(result['stripped_edges'])} dangling edge(s): {edges}"
    print(msg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
