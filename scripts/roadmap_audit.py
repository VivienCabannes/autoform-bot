#!/usr/bin/env python3
"""Roadmap completeness audit — "is the roadmap complete?" as a computed answer.

The graph is only fleet fuel when its gaps are machine-checkable: an AI fleet
cannot drain "the planner should double-check chapter 3", but it can drain a
queue of concrete offenders. This script evaluates the roadmap against named
clauses and (with ``--enqueue``) turns every failure into a queued role task,
so completeness becomes a frontier the workers advance exactly like proofs.

Clauses (each yields offenders ``{node, detail}``):

  structural     scripts/check_invariants.py's gates (references, tiers, cycles)
  status         every ``mathlib_status`` normalizes to in-mathlib|partial|missing
  grounding      every missing node reaches an in-mathlib root (normalized)
  verified       in-mathlib claims carry declarations + a verification stamp —
                 an UNVERIFIED in-mathlib status silently poisons the trust
                 frontier (``is_trusted`` believes it by construction)
  content        tier-2 nodes have prose on disk (and no orphaned prose files)
  provenance     non-in-mathlib nodes cite at least one source_ref
  slugs          no two node ids collide onto one canonical wiki path
  targets        metadata.targets resolve to tier-2 nodes with grounded cones
  leanpaths      lean_file stays inside the Lean repo and exists once claimed

Verification (``--verify-decls``) greps each claimed declaration in the local
Mathlib checkout (full name, then last component behind a declaration keyword —
a heuristic; the mathlib-checker role stays the authority). ``--stamp-verified``
writes a ``mathlib_verified`` stamp through merge_node for nodes whose every
declaration resolved, so downstream tooling can distinguish checked claims
from guesses.

Enqueue mapping (``--enqueue``, deduplicated by the queue itself):
  status/verified → mathcheck · grounding/targets → graphreview ·
  content/provenance/slugs → contentreview · leanpaths → escalation

Exit code: 0 = no offenders, 1 = offenders found, 2 = cannot audit.
Usage:
    roadmap_audit.py <graph.json> [--json] [--enqueue] [--verify-decls]
                     [--stamp-verified] [--mathlib PATH]
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _p in (_HERE, _HERE / "review_ui", _HERE.parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import check_invariants  # noqa: E402
import export_blueprint as eb  # noqa: E402
import review_model as rm  # noqa: E402

_DECL_KEYWORDS = r"(?:theorem|lemma|def|abbrev|structure|class|instance|inductive|opaque)"

CLAUSE_KIND = {
    "status": "mathcheck",
    "verified": "mathcheck",
    "grounding": "graphreview",
    "targets": "graphreview",
    "content": "contentreview",
    "provenance": "contentreview",
    "slugs": "contentreview",
    "leanpaths": "escalation",
}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _offender(node: str, detail: str) -> dict:
    return {"node": node, "detail": detail}


# ---------------------------------------------------------------------------
# clauses
# ---------------------------------------------------------------------------


def audit_structural(graph_path: Path) -> list[dict]:
    """check_invariants' structural gates, captured instead of printed."""
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        structural_ok, _grounded_ok = check_invariants.check(str(graph_path))
    if structural_ok:
        return []
    lines = [ln.strip() for ln in buffer.getvalue().splitlines() if ln.strip().startswith("✗")]
    return [_offender("(graph)", line) for line in lines] or [
        _offender("(graph)", "structural check failed — run check_invariants.py for detail")
    ]


def audit_status(nodes: dict) -> list[dict]:
    out = []
    for nid, node in nodes.items():
        raw = node.get("mathlib_status")
        norm = rm.normalize_status(raw)
        if norm is None:
            out.append(_offender(nid, f"unrecognized mathlib_status {raw!r}"))
        elif raw != norm:
            out.append(_offender(nid, f"non-canonical spelling {raw!r} → {norm!r}"))
    return out


def audit_grounding(nodes: dict) -> list[dict]:
    """Every missing node reaches an in-mathlib root — with NORMALIZED statuses,
    so a graph written with `exists` audits the same way the dashboard reads it."""
    grounded: dict[str, bool] = {}

    def grounds(nid: str, seen: frozenset) -> bool:
        if nid in grounded:
            return grounded[nid]
        if nid in seen:
            return False
        node = nodes[nid]
        if rm.normalize_status(node.get("mathlib_status")) == "in-mathlib":
            grounded[nid] = True
            return True
        result = any(dep in nodes and grounds(dep, seen | {nid}) for dep in node.get("depends_on") or [])
        grounded[nid] = result
        return result

    return [
        _offender(nid, "missing node with no in-mathlib root in its dependency closure")
        for nid, node in nodes.items()
        if eb.node_tier(node) == 2
        and rm.normalize_status(node.get("mathlib_status")) == "missing"
        and not grounds(nid, frozenset())
    ]


def audit_verified(
    nodes: dict, lean_root: Path | None, verify_decls: bool, mathlib_override: str | None
) -> tuple[list[dict], dict[str, dict]]:
    """In-mathlib claims must be evidence-backed. Returns (offenders, stampable)."""
    offenders: list[dict] = []
    stampable: dict[str, dict] = {}
    searcher = None
    if verify_decls and lean_root is not None:
        try:
            from servers.mathlib import core as mathlib_core  # noqa: PLC0415

            root = Path(mathlib_override) if mathlib_override else lean_root
            if mathlib_core.find_mathlib_path(root) is not None:
                searcher = (mathlib_core, root)
        except Exception:
            searcher = None

    for nid, node in nodes.items():
        if rm.normalize_status(node.get("mathlib_status")) != "in-mathlib":
            continue
        decls = [d for d in (node.get("mathlib_declarations") or []) if isinstance(d, str) and d]
        if not decls:
            offenders.append(_offender(nid, "in-mathlib with no mathlib_declarations — unverifiable claim"))
            continue
        if node.get("mathlib_verified"):
            continue
        if searcher is None:
            offenders.append(
                _offender(
                    nid, "in-mathlib but never verified (no stamp; run with --verify-decls near a Mathlib checkout)"
                )
            )
            continue
        core, root = searcher
        missing = [d for d in decls if not _decl_found(core, root, d)]
        if missing:
            offenders.append(_offender(nid, f"claimed declaration(s) not found in Mathlib: {', '.join(missing[:3])}"))
        else:
            stampable[nid] = {"at": _now(), "method": "grep", "declarations": len(decls)}
    return offenders, stampable


def _decl_found(core, root: Path, name: str) -> bool:
    try:
        exact = core.grep_mathlib(root, pattern=rf"\b{re.escape(name)}\b", max_results=1)
        if _has_match(exact):
            return True
        tail = name.rsplit(".", 1)[-1]
        kw = core.grep_mathlib(root, pattern=rf"{_DECL_KEYWORDS}\s+{re.escape(tail)}\b", max_results=1)
        return _has_match(kw)
    except Exception:
        return False


def _has_match(output: str) -> bool:
    text = (output or "").strip()
    return bool(text) and "no matches" not in text.lower()


def audit_content(nodes: dict, project: Path) -> list[dict]:
    out = []
    referenced: set[str] = set()
    for nid, node in nodes.items():
        if eb.node_tier(node) != 2:
            continue
        content = node.get("content")
        if not content:
            out.append(_offender(nid, "tier-2 node with null content — no prose for provers/reviewers"))
            continue
        referenced.add(content)
        path = (project / content).resolve(strict=False)
        try:
            path.relative_to(project.resolve())
        except ValueError:
            out.append(_offender(nid, f"content path escapes project: {content}"))
            continue
        if not path.exists() or not path.read_text(encoding="utf-8", errors="replace").strip():
            out.append(_offender(nid, f"content file missing or empty: {content}"))
    for relative_dir in (Path("wiki/nodes"), Path("informal_content")):
        prose_dir = project / relative_dir
        if prose_dir.is_dir():
            for orphan in sorted(prose_dir.glob("*.md")):
                if orphan.name == "README.md":
                    continue
                rel = (relative_dir / orphan.name).as_posix()
                if rel not in referenced:
                    out.append(_offender("(orphan)", f"prose file no node references: {rel}"))
    return out


_ORIGINS = ("cited", "bridged", "background")


def audit_provenance(nodes: dict, metadata: dict | None = None) -> list[dict]:
    """Provenance = declared ORIGIN, not mandatory citations.

    The blueprint is a unified argument its agents AUTHOR (leanblueprint-style):
    gap-bridging and standard background written from the agent's own
    mathematical knowledge are legitimate — what is never legitimate is
    ambiguity about which kind a statement is, or a citation that doesn't
    resolve. ``origin``: ``cited`` (from the corpus — needs ``source_refs``),
    ``bridged`` (agent-authored connective mathematics — gets extra adversarial
    review), ``background`` (standard material). A node with ``source_refs``
    and no explicit origin is treated as ``cited``.
    """
    out = []
    sources = metadata.get("sources", []) if isinstance(metadata, dict) else []
    registered = {
        value
        for source in sources
        if isinstance(source, dict)
        for value in (source.get("id"), source.get("file"), source.get("citation_key"))
        if isinstance(value, str) and value
    }
    for nid, node in nodes.items():
        if eb.node_tier(node) != 2 or rm.normalize_status(node.get("mathlib_status")) == "in-mathlib":
            continue
        origin = node.get("origin")
        refs = node.get("source_refs") or []
        if origin is None:
            origin = "cited" if refs else None
        if origin is None:
            out.append(
                _offender(
                    nid,
                    "no origin declared and no source_refs — say whether this is cited, "
                    "bridged (agent-authored), or background",
                )
            )
        elif origin not in _ORIGINS:
            out.append(_offender(nid, f"unknown origin {origin!r} (cited|bridged|background)"))
        elif origin == "cited" and not refs:
            out.append(_offender(nid, "origin 'cited' but no source_refs — cite it or mark it bridged"))
        for ref in refs:
            if isinstance(ref, dict) and isinstance(ref.get("source"), str):
                if ref["source"] not in registered:
                    out.append(_offender(nid, f"source_ref names unregistered source {ref['source']!r}"))
    return out


def audit_slugs(nodes: dict) -> list[dict]:
    """Group by the RAW slug of each id, not build_slug_map — that map is
    collision-free by construction (it appends _2, _3, …), which is exactly the
    silent renaming this clause exists to surface."""
    by_slug: dict[str, list[str]] = {}
    for nid in nodes:
        by_slug.setdefault(eb.make_slug(nid), []).append(nid)
    return [
        _offender(" / ".join(sorted(ids)), f"slug collision on {slug!r} — prose files overwrite each other")
        for slug, ids in sorted(by_slug.items())
        if len(ids) > 1
    ]


def audit_targets(nodes: dict, meta: dict) -> list[dict]:
    out = []
    targets = rm.graph_targets(meta)
    for target in targets:
        if target not in nodes:
            out.append(_offender(target, "metadata.targets entry does not resolve to a node"))
            continue
        if eb.node_tier(nodes[target]) != 2:
            out.append(_offender(target, "target must be a tier-2 statement node"))
            continue
        cone = rm.dependency_cone(target, nodes)
        ungrounded = [
            nid
            for nid in cone
            if rm.normalize_status(nodes[nid].get("mathlib_status")) == "missing"
            and not any(
                rm.normalize_status(nodes[d].get("mathlib_status")) == "in-mathlib"
                for d in rm.dependency_cone(nid, nodes)
            )
        ]
        for nid in sorted(ungrounded):
            out.append(_offender(nid, f"in target {target!r}'s cone but reaches no in-mathlib root"))
    return out


def audit_leanpaths(nodes: dict, lean_root: Path | None) -> list[dict]:
    out = []
    for nid, node in nodes.items():
        lean_file = node.get("lean_file")
        if not lean_file or lean_root is None:
            continue
        path = lean_root / lean_file
        try:
            path.resolve().relative_to(lean_root.resolve())
        except ValueError:
            out.append(_offender(nid, f"lean_file escapes the Lean repo: {lean_file}"))
            continue
        if not path.exists():
            out.append(_offender(nid, f"lean_file claimed but absent: {lean_file}"))
    return out


# ---------------------------------------------------------------------------
# assembly / enqueue / stamp
# ---------------------------------------------------------------------------


def run_audit(graph_path: Path, verify_decls: bool = False, mathlib_override: str | None = None) -> tuple[dict, dict]:
    nodes, meta = rm.load_graph(graph_path)
    project = graph_path.parent
    lean_root = None
    raw_root = (meta or {}).get("lean_root")
    if raw_root and Path(raw_root).is_dir():
        lean_root = Path(raw_root)

    verified, stampable = audit_verified(nodes, lean_root, verify_decls, mathlib_override)
    clauses = {
        "structural": audit_structural(graph_path),
        "status": audit_status(nodes),
        "grounding": audit_grounding(nodes),
        "verified": verified,
        "content": audit_content(nodes, project),
        "provenance": audit_provenance(nodes, meta or {}),
        "slugs": audit_slugs(nodes),
        "targets": audit_targets(nodes, meta or {}),
        "leanpaths": audit_leanpaths(nodes, lean_root),
    }
    report = {
        "graph": str(graph_path),
        "at": _now(),
        "clauses": clauses,
        "summary": {name: len(offs) for name, offs in clauses.items()},
        "targets": {
            t: rm.target_metrics(t, nodes, rm.load_sidecar(project / "review_status.json"))
            for t in rm.graph_targets(meta or {})
            if t in nodes
        },
        "ok": not any(clauses.values()),
    }
    return report, stampable


def enqueue_offenders(project: Path, clauses: dict) -> int:
    import dispatch_queue as dq  # noqa: PLC0415

    added = 0
    for clause, offenders in clauses.items():
        kind = CLAUSE_KIND.get(clause)
        if kind is None:
            continue  # structural failures block; they are not agent work
        for off in offenders:
            node = off["node"]
            if node.startswith("("):
                continue
            rc = dq.main(
                [
                    str(project),
                    "enqueue",
                    "--agent",
                    kind,
                    "--node",
                    node,
                    "--note",
                    f"audit:{clause} — {off['detail']}",
                    "--source",
                    "engine",
                ]
            )
            if rc == 0:
                added += 1
    return added


def stamp_verified(graph_path: Path, nodes: dict, stampable: dict) -> int:
    if not stampable:
        return 0
    import merge_node  # noqa: PLC0415

    payload = {"upsert": {nid: {**nodes[nid], "mathlib_verified": stamp} for nid, stamp in stampable.items()}}
    merge_node.merge(str(graph_path), payload)
    return len(stampable)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Audit roadmap completeness; optionally queue the gaps.")
    ap.add_argument("graph", type=Path)
    ap.add_argument("--json", action="store_true", help="machine-readable report")
    ap.add_argument("--enqueue", action="store_true", help="queue one role task per auditable offender (deduplicated)")
    ap.add_argument(
        "--verify-decls", action="store_true", help="grep claimed declarations in the local Mathlib checkout"
    )
    ap.add_argument(
        "--stamp-verified",
        action="store_true",
        help="write mathlib_verified stamps for fully-resolving nodes (implies --verify-decls)",
    )
    ap.add_argument("--mathlib", default=None, help="explicit Mathlib checkout root")
    args = ap.parse_args(argv)

    if not args.graph.exists():
        print(f"no graph at {args.graph}", file=sys.stderr)
        return 2
    report, stampable = run_audit(
        args.graph, verify_decls=args.verify_decls or args.stamp_verified, mathlib_override=args.mathlib
    )

    if args.stamp_verified and stampable:
        nodes, _meta = rm.load_graph(args.graph)
        stamped = stamp_verified(args.graph, nodes, stampable)
        report["stamped"] = stamped
    if args.enqueue:
        report["enqueued"] = enqueue_offenders(args.graph.parent, report["clauses"])

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        for name, offenders in report["clauses"].items():
            mark = "✓" if not offenders else "✗"
            print(f"  {mark} {name:11} {len(offenders)} offender(s)")
            for off in offenders[:8]:
                print(f"      • {off['node']}: {off['detail']}")
            if len(offenders) > 8:
                print(f"      … {len(offenders) - 8} more")
        for target, metrics in (report.get("targets") or {}).items():
            print(
                f"  ◎ target {target}: cone {metrics['cone_size']}, "
                f"unproved {metrics['unproved_mass']}, ready {metrics['ready']}, "
                f"critical path {metrics['critical_path']}"
            )
        if "stamped" in report:
            print(f"  stamped {report['stamped']} verified in-mathlib node(s)")
        if "enqueued" in report:
            print(f"  enqueued {report['enqueued']} gap task(s)")
        print("roadmap audit:", "OK" if report["ok"] else "INCOMPLETE")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
