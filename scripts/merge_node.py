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
internal/references/plan-json-schema.md. An upserted record's "id" must match
its key (it is filled in from the key when omitted). Deleting a node strips it
from every other node's ``depends_on`` and sets its children's ``parent`` to
null. New unresolved references are rejected atomically instead of being
silently discarded; the caller can retry after the prerequisite lands.

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
            if not isinstance(rec, dict):
                raise ValueError(f"upsert record must be an object: {rec!r}")
            nid = rec.get("id")
            if not isinstance(nid, str) or not nid:
                raise ValueError(f"upsert record missing 'id': {rec!r}")
            if nid in out:
                raise ValueError(f"duplicate upsert id: {nid!r}")
            out[nid] = rec
        return out
    if isinstance(upsert, dict):
        for key, rec in upsert.items():
            if not isinstance(key, str) or not key:
                raise ValueError(f"upsert key must be a non-empty string: {key!r}")
            if not isinstance(rec, dict):
                raise ValueError(f"upsert record for {key!r} must be an object")
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
    raw_deletes = payload.get("delete", [])
    if not isinstance(raw_deletes, list) or not all(
        isinstance(node_id, str) and node_id for node_id in raw_deletes
    ):
        raise ValueError("'delete' must be a list of non-empty node-id strings")
    if len(raw_deletes) != len(set(raw_deletes)):
        raise ValueError("'delete' contains duplicate node ids")
    deletes = list(raw_deletes)

    with open(graph_path, encoding="utf-8") as f:
        graph = json.load(f)
    nodes = graph.setdefault("nodes", {})
    if not isinstance(nodes, dict):
        raise ValueError("graph.json must contain a nodes object")

    for nid in deletes:
        nodes.pop(nid, None)
    for nid, rec in upserts.items():
        nodes[nid] = rec

    # Deletion cleanup is explicit. Any OTHER missing reference is rejected:
    # silently dropping a new edge when concurrently merged prerequisites land
    # in the opposite order loses mathematical meaning.
    deleted = set(deletes)
    stripped: list[tuple[str, str]] = []
    orphaned: list[tuple[str, str]] = []
    for nid, rec in nodes.items():
        if not isinstance(rec, dict):
            raise ValueError(f"node {nid!r} must be an object")
        parent = rec.get("parent")
        if parent is not None and parent not in nodes:
            if parent in deleted:
                rec["parent"] = None
                orphaned.append((nid, parent))
            else:
                raise ValueError(
                    f"node {nid!r} references absent parent {parent!r}; "
                    "merge the parent in the same payload or first"
                )
        deps = rec.get("depends_on")
        if not deps:
            continue
        if not isinstance(deps, list) or not all(isinstance(dep, str) for dep in deps):
            raise ValueError(f"node {nid!r} depends_on must be a list of strings")
        missing = [dep for dep in deps if dep not in nodes]
        unresolved = [dep for dep in missing if dep not in deleted]
        if unresolved:
            raise ValueError(
                f"node {nid!r} references absent dependencies {unresolved!r}; "
                "merge prerequisites in the same payload or first"
            )
        if missing:
            stripped.extend((nid, dep) for dep in missing)
            rec["depends_on"] = [dep for dep in deps if dep in nodes]

    # Optional metadata merge — currently ONLY the mission targets. Targets are
    # first-class graph state (the fleet's prove ordering and the audit's
    # reachability clause read them), so they go through the single writer like
    # every other structural change. Each entry must resolve to a node that
    # exists after this payload's upserts/deletes.
    meta_patch = payload.get("metadata")
    targets_set = None
    if meta_patch is not None:
        if not isinstance(meta_patch, dict) or set(meta_patch) - {"targets"}:
            raise ValueError("payload 'metadata' may only set 'targets'")
        raw_targets = meta_patch.get("targets")
        if not isinstance(raw_targets, list):
            raise ValueError("'metadata.targets' must be a list")
        normalized: list = []
        for entry in raw_targets:
            node_id = entry if isinstance(entry, str) else (
                entry.get("node") if isinstance(entry, dict) else None)
            if not isinstance(node_id, str) or not node_id:
                raise ValueError(f"invalid targets entry {entry!r}")
            if node_id not in nodes:
                raise ValueError(f"targets entry {node_id!r} does not resolve to a node")
            normalized.append(entry)
        graph.setdefault("metadata", {})["targets"] = normalized
        targets_set = len(normalized)

    graph.setdefault("metadata", {})["last_updated"] = _now()

    _atomic_write(graph_path, graph)
    result = {
        "upserted": len(upserts),
        "deleted": len(deletes),
        "stripped_edges": stripped,
        "orphaned_children": orphaned,
        "total_nodes": len(nodes),
    }
    if targets_set is not None:
        result["targets_set"] = targets_set
    return result


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

    try:
        payload = _load_payload(args.payload)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        print(f"merge rejected: {error}", file=sys.stderr)
        return 2
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
            try:
                result = merge(args.graph, payload)
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
                print(f"merge rejected: {error}", file=sys.stderr)
                return 2
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
    if result["orphaned_children"]:
        children = ", ".join(
            f"{child} -/-> {parent}"
            for child, parent in result["orphaned_children"]
        )
        msg += (
            f"; orphaned {len(result['orphaned_children'])} child(ren): {children}"
        )
    print(msg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
