"""Shared schema-v4 graph helpers.

The top-level ``edges`` table is the canonical relationship store.  The
per-node dependency arrays remain a materialized compatibility view for the
scheduler and older tools.
"""

from __future__ import annotations

import hashlib
from typing import Any

SCHEMA_VERSION = 4

EDGE_FIELDS = {
    "statement-requires": "statement_depends_on",
    "proof-requires": "proof_depends_on",
    "related": "related",
}
HARD_EDGE_KINDS = frozenset(("statement-requires", "proof-requires"))
SOFT_EDGE_KINDS = frozenset(("related", "generalizes", "special-case"))
EDGE_KINDS = HARD_EDGE_KINDS | SOFT_EDGE_KINDS
CONFIDENCE_LEVELS = frozenset(("high", "medium", "low", "unknown"))


def edge_id(source: str, target: str, kind: str) -> str:
    """Return a stable, path-safe identity for one aggregate cell edge."""
    payload = f"{kind}\0{source}\0{target}".encode("utf-8")
    return "edge:" + hashlib.sha256(payload).hexdigest()[:20]


def _string_list(value: Any, label: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"{label} must be a list of non-empty strings")
    return list(dict.fromkeys(value))


def normalize_aliases(node_id: str, node: dict[str, Any]) -> None:
    node["aliases"] = _string_list(node.get("aliases"), f"node {node_id!r} aliases")


def normalize_edge(raw: Any, nodes: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"edge must be an object: {raw!r}")
    edge = dict(raw)
    source = edge.get("source")
    target = edge.get("target")
    kind = edge.get("kind")
    if not isinstance(source, str) or not source:
        raise ValueError("edge source must be a non-empty node id")
    if not isinstance(target, str) or not target:
        raise ValueError("edge target must be a non-empty node id")
    if kind not in EDGE_KINDS:
        raise ValueError(f"edge {source!r} -> {target!r} has unsupported kind {kind!r}")
    if source not in nodes or target not in nodes:
        missing = [node_id for node_id in (source, target) if node_id not in nodes]
        raise ValueError(f"edge {source!r} -> {target!r} references absent nodes {missing!r}")
    expected = edge_id(source, target, kind)
    supplied = edge.get("id")
    if supplied is not None and supplied != expected:
        raise ValueError(f"edge id {supplied!r} does not match canonical id {expected!r}")
    edge["id"] = expected
    confidence = edge.setdefault("confidence", "unknown")
    if confidence not in CONFIDENCE_LEVELS:
        raise ValueError(f"edge {expected!r} has invalid confidence {confidence!r}")
    provenance = edge.setdefault("provenance", {"kind": "unspecified"})
    if not isinstance(provenance, dict):
        raise ValueError(f"edge {expected!r} provenance must be an object")
    evidence = edge.get("evidence")
    if evidence is not None and not isinstance(evidence, (dict, list, str)):
        raise ValueError(f"edge {expected!r} evidence must be an object, list, string, or null")
    traces = edge.get("traces")
    if traces is not None and (
        not isinstance(traces, list) or not all(isinstance(trace, dict) for trace in traces)
    ):
        raise ValueError(f"edge {expected!r} traces must be a list of objects")
    return edge


def edges_from_node_fields(nodes: dict[str, Any]) -> list[dict[str, Any]]:
    """Lift legacy node-local arrays into evidence-bearing aggregate edges."""
    edges: list[dict[str, Any]] = []
    for source, node in nodes.items():
        if not isinstance(node, dict):
            raise ValueError(f"node {source!r} must be an object")
        has_typed = any(field in node for field in ("statement_depends_on", "proof_depends_on"))
        fields = (
            ("statement-requires", node.get("statement_depends_on")),
            ("proof-requires", node.get("proof_depends_on")),
        )
        if not has_typed:
            fields = (("proof-requires", node.get("depends_on")),)
        fields += (("related", node.get("related")),)
        for kind, values in fields:
            for target in _string_list(values, f"node {source!r} {EDGE_FIELDS[kind]}"):
                edges.append(
                    normalize_edge(
                        {
                            "source": source,
                            "target": target,
                            "kind": kind,
                            "confidence": "unknown",
                            "provenance": {"kind": "legacy-node-field"},
                        },
                        nodes,
                    )
                )
    return edges


def normalize_edges(graph: dict[str, Any], *, migrate: bool = False) -> list[dict[str, Any]]:
    nodes = graph.get("nodes")
    if not isinstance(nodes, dict):
        raise ValueError("graph.json must contain a nodes object")
    raw_edges = graph.get("edges")
    if raw_edges is None:
        if not migrate and graph.get("version", 0) >= SCHEMA_VERSION:
            raise ValueError("schema-v4 graph must contain an edges list")
        raw_edges = edges_from_node_fields(nodes)
    if not isinstance(raw_edges, list):
        raise ValueError("graph.json edges must be a list")
    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_relations: set[tuple[str, str, str]] = set()
    for raw in raw_edges:
        edge = normalize_edge(raw, nodes)
        relation = (edge["source"], edge["target"], edge["kind"])
        if edge["id"] in seen_ids or relation in seen_relations:
            raise ValueError(f"duplicate graph edge {relation!r}")
        seen_ids.add(edge["id"])
        seen_relations.add(relation)
        normalized.append(edge)
    return normalized


def project_edges(graph: dict[str, Any], edges: list[dict[str, Any]] | None = None) -> None:
    """Materialize canonical edges into scheduler-compatible node fields."""
    nodes = graph["nodes"]
    canonical = edges if edges is not None else normalize_edges(graph)
    for node_id, node in nodes.items():
        if not isinstance(node, dict):
            raise ValueError(f"node {node_id!r} must be an object")
        node["statement_depends_on"] = []
        node["proof_depends_on"] = []
        node["related"] = []
        normalize_aliases(node_id, node)
    for edge in canonical:
        field = EDGE_FIELDS.get(edge["kind"])
        if field is not None:
            nodes[edge["source"]][field].append(edge["target"])
    for node in nodes.values():
        node["depends_on"] = list(
            dict.fromkeys(node["statement_depends_on"] + node["proof_depends_on"])
        )
    graph["edges"] = canonical


def migrate_graph(graph: dict[str, Any]) -> None:
    """Upgrade an in-memory graph to v4 without discarding legacy semantics."""
    edges = normalize_edges(graph, migrate=True)
    graph["version"] = SCHEMA_VERSION
    project_edges(graph, edges)


def alias_index(nodes: dict[str, Any]) -> dict[str, list[str]]:
    """Build a deterministic, ambiguity-preserving resolver index."""
    index: dict[str, set[str]] = {}
    for node_id, node in nodes.items():
        aliases = _string_list(node.get("aliases"), f"node {node_id!r} aliases")
        candidates = [node_id, node.get("name"), node.get("title"), *aliases]
        for key in ("lean_declaration",):
            candidates.append(node.get(key))
        candidates.extend(node.get("mathlib_declarations") or [])
        for ref in node.get("source_refs") or []:
            if not isinstance(ref, dict):
                continue
            source = ref.get("source")
            locator = ref.get("locator")
            if isinstance(locator, str) and locator:
                candidates.append(locator)
                if isinstance(source, str) and source:
                    candidates.append(f"{source}:{locator}")
        for candidate in candidates:
            if isinstance(candidate, str) and candidate.strip():
                index.setdefault(candidate.strip(), set()).add(node_id)
    return {key: sorted(values) for key, values in sorted(index.items())}


def resolve_alias(nodes: dict[str, Any], key: str) -> str:
    matches = alias_index(nodes).get(key, [])
    if not matches:
        raise ValueError(f"no cell matches {key!r}")
    if len(matches) != 1:
        raise ValueError(f"cell alias {key!r} is ambiguous: {matches!r}")
    return matches[0]
