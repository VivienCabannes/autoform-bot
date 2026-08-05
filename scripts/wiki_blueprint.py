#!/usr/bin/env python3
"""Create, migrate, and deterministically render Autoform's project wiki.

``graph.json`` is the canonical control plane. Authored Markdown under ``wiki``
is the canonical mathematical narrative. Files under ``wiki/_generated`` are
derived navigation and may be deleted and rebuilt at any time.

The migration accepts both historical Autoform layouts:

* schema-v2 ``informal_content/*.md`` projects; and
* Markdown-first ``blueprint/roadmap/**/*.md`` projects.

It never silently replaces authored files. Run ``migrate`` explicitly before
changing an existing project; ``init`` and ``build`` are non-destructive.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

try:
    from .graph_contract import (
        SCHEMA_VERSION,
        alias_index,
        migrate_graph,
        normalize_edges,
        resolve_alias,
    )
except ImportError:  # direct script execution
    from graph_contract import (
        SCHEMA_VERSION,
        alias_index,
        migrate_graph,
        normalize_edges,
        resolve_alias,
    )

AUTHORED_DIRS = ("nodes", "sources", "papers", "concepts", "audits", "decisions")
DEPENDENCY_FIELDS = ("statement_depends_on", "proof_depends_on")

_ROOT_README = """# Autoform Wiki

This is the durable mathematical knowledge base for the formalization.

- [Nodes](nodes/README.md) contain the canonical informal statements and proofs.
- [Sources](sources/README.md) record stable external references and locators.
- [Concepts](concepts/README.md) synthesize recurring mathematical ideas.
- [Audits](audits/README.md) preserve durable mathematical review findings.
- [Decisions](decisions/README.md) explain important modeling choices.
- [_generated](_generated/index.md) is the rebuildable cell/supercell view of the DAG.

`graph.json` owns cell identity, hierarchy, evidence-bearing edges, targets,
and machine state. Authored Markdown is each cell's mathematical body. Do not hand-edit files
under `_generated/`, and do not record queues, agent logs, credentials, local
paths, or provider configuration anywhere in this wiki.
"""

_SECTION_READMES = {
    "nodes": "# Nodes\n\nCanonical informal statements and proofs, one file per graph node.\n",
    "sources": "# Sources\n\nSource maps, stable links, citation keys, and precise locators.\n",
    "papers": "# Papers\n\nPaper-level notes keyed by stable citation identifiers.\n",
    "concepts": "# Concepts\n\nCross-node mathematical synthesis and notation.\n",
    "audits": "# Audits\n\nDurable review conclusions and unresolved mathematical risks.\n",
    "decisions": "# Decisions\n\nModeling and formalization decisions with their rationale.\n",
}


class WikiError(ValueError):
    """A project cannot be read or migrated safely."""


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "node"


def _slug_map(ids: list[str]) -> dict[str, str]:
    grouped: dict[str, list[str]] = {}
    for node_id in ids:
        grouped.setdefault(slugify(node_id), []).append(node_id)
    result: dict[str, str] = {}
    for base, members in grouped.items():
        for node_id in sorted(members):
            suffix = "" if len(members) == 1 else "-" + hashlib.sha256(node_id.encode("utf-8")).hexdigest()[:8]
            result[node_id] = base + suffix
    return result


def _safe_path(project: Path, relative: str | Path, *, label: str) -> Path:
    raw = PurePosixPath(str(relative).replace("\\", "/"))
    if raw.is_absolute() or ".." in raw.parts:
        raise WikiError(f"{label} escapes the project: {relative}")
    project = project.resolve()
    candidate = (project / Path(*raw.parts)).resolve(strict=False)
    try:
        candidate.relative_to(project)
    except ValueError as error:
        raise WikiError(f"{label} escapes the project: {relative}") from error
    return candidate


def _write_if_missing(path: Path, text: str) -> None:
    if path.exists():
        if not path.is_file() or path.is_symlink():
            raise WikiError(f"expected a regular file at {path}")
        return
    path.write_text(text, encoding="utf-8")


def ensure_layout(project: Path) -> Path:
    project = project.resolve()
    if not project.is_dir():
        raise WikiError(f"project directory does not exist: {project}")
    wiki = project / "wiki"
    if wiki.is_symlink():
        raise WikiError(f"refusing symlinked wiki directory: {wiki}")
    wiki.mkdir(exist_ok=True)
    _write_if_missing(wiki / "README.md", _ROOT_README)
    for name in AUTHORED_DIRS:
        section = wiki / name
        if section.is_symlink():
            raise WikiError(f"refusing symlinked wiki section: {section}")
        section.mkdir(exist_ok=True)
        _write_if_missing(section / "README.md", _SECTION_READMES[name])
    return wiki


def _load_graph(path: Path) -> dict[str, Any]:
    try:
        graph = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WikiError(f"cannot read graph at {path}: {error}") from error
    if not isinstance(graph, dict) or not isinstance(graph.get("nodes"), dict):
        raise WikiError("graph.json must be an object with a nodes object")
    if not isinstance(graph.setdefault("metadata", {}), dict):
        raise WikiError("graph.json metadata must be an object")
    return graph


def _write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _snapshot_migration(project: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = project / ".autoform" / "snapshots"
    root.mkdir(parents=True, exist_ok=True)
    snapshot = root / f"wiki-v4-{stamp}-{os.getpid()}"
    snapshot.mkdir()
    shutil.copy2(project / "graph.json", snapshot / "graph.json")
    for name in ("wiki", "informal_content"):
        source = project / name
        if source.is_dir() and not source.is_symlink():
            shutil.copytree(source, snapshot / name)
    return snapshot


def _frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    data: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if ":" not in line or line.lstrip().startswith("#"):
            continue
        key, value = line.split(":", 1)
        data[key.strip()] = value.strip().strip("'\"")
    return data, text[end + 5 :]


def _heading(text: str, fallback: str) -> str:
    match = re.search(r"^#\s+(.+?)\s*$", text, re.MULTILINE)
    return match.group(1).strip() if match else fallback


def _section_links(text: str, heading: str, source: Path, root: Path) -> list[str]:
    pattern = rf"^##\s+{re.escape(heading)}\s*$\n(.*?)(?=^##\s|\Z)"
    match = re.search(pattern, text, re.MULTILINE | re.DOTALL | re.IGNORECASE)
    if not match:
        return []
    links: list[str] = []
    for target in re.findall(r"\[[^]]+\]\(([^)#]+\.md)(?:#[^)]+)?\)", match.group(1)):
        resolved = (source.parent / target).resolve(strict=False)
        try:
            relative = resolved.relative_to(root.resolve()).with_suffix("")
        except ValueError:
            continue
        node_id = relative.as_posix()
        if node_id.endswith("/README"):
            node_id = node_id[: -len("/README")]
        links.append(node_id)
    return list(dict.fromkeys(links))


def _legacy_source_refs(text: str, source: Path, project: Path) -> list[dict[str, str]]:
    match = re.search(
        r"^##\s+Sources\s*$\n(.*?)(?=^##\s|\Z)",
        text,
        re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    if not match:
        return []
    root = (project / "blueprint" / "sources").resolve()
    refs: list[dict[str, str]] = []
    for label, target in re.findall(r"\[([^]]+)\]\(([^)#]+\.md)(?:#[^)]+)?\)", match.group(1)):
        resolved = (source.parent / target).resolve(strict=False)
        try:
            relative = resolved.relative_to(root).with_suffix("")
        except ValueError:
            continue
        refs.append(
            {
                "source": slugify(relative.as_posix()),
                "locator": label.replace("`", "").strip(),
                "role": "statement",
            }
        )
    return refs


def _import_blueprint_sources(project: Path, wiki: Path, metadata: dict) -> int:
    root = project / "blueprint" / "sources"
    if not root.is_dir() or root.is_symlink():
        return 0
    sources = metadata.setdefault("sources", [])
    if not isinstance(sources, list):
        raise WikiError("metadata.sources must be a list")
    existing = {
        source.get("id") for source in sources if isinstance(source, dict) and isinstance(source.get("id"), str)
    }
    imported = 0
    for path in sorted(root.rglob("*.md")):
        if path.is_symlink():
            raise WikiError(f"refusing symlinked blueprint source page: {path}")
        text = path.read_text(encoding="utf-8")
        front, body = _frontmatter(text)
        if front.get("kind") not in (None, "source"):
            continue
        source_id = slugify(path.relative_to(root).with_suffix("").as_posix())
        destination = wiki / "sources" / f"{source_id}.md"
        if not destination.exists():
            destination.write_text(body.lstrip(), encoding="utf-8")
        if source_id in existing:
            continue
        url_match = re.search(r"https?://[^)\s]+", body)
        record: dict[str, str] = {
            "id": source_id,
            "title": _heading(body, path.stem.replace("-", " ").title()),
            "wiki": destination.relative_to(project).as_posix(),
        }
        if url_match:
            record["url"] = url_match.group(0).rstrip(".,")
        sources.append(record)
        existing.add(source_id)
        imported += 1
    return imported


def _legacy_blueprint_nodes(project: Path) -> dict[str, tuple[dict, str]]:
    root = project / "blueprint" / "roadmap"
    if not root.is_dir() or root.is_symlink():
        return {}
    found: dict[str, tuple[dict, str]] = {}
    for path in sorted(root.rglob("*.md")):
        if path.is_symlink():
            raise WikiError(f"refusing symlinked blueprint page: {path}")
        text = path.read_text(encoding="utf-8")
        front, body = _frontmatter(text)
        if front.get("kind") != "node":
            continue
        relative = path.relative_to(root).with_suffix("")
        node_id = relative.as_posix()
        statement = _section_links(body, "Depends on", path, root)
        proof = _section_links(body, "Proof depends on", path, root)
        cluster_id = f"cluster:{relative.parts[0]}"
        if cluster_id not in found:
            found[cluster_id] = (
                {
                    "id": cluster_id,
                    "name": relative.parts[0].replace("-", " ").title(),
                    "tier": 1,
                    "parent": None,
                    "kind": "section",
                    "statement_depends_on": [],
                    "proof_depends_on": [],
                    "depends_on": [],
                    "related": [],
                    "mathlib_status": "partial",
                    "origin": "background",
                },
                "",
            )
        record: dict[str, Any] = {
            "id": node_id,
            "name": _heading(body, path.stem.replace("-", " ").title()),
            "tier": 2,
            "parent": cluster_id,
            "kind": front.get("declaration", "theorem"),
            "statement_depends_on": statement,
            "proof_depends_on": proof,
            "depends_on": list(dict.fromkeys(statement + proof)),
            "mathlib_status": "in-mathlib" if front.get("mathlib") == "true" else "missing",
            "origin": "cited" if "## Sources" in body else "background",
            "source_refs": _legacy_source_refs(body, path, project),
        }
        if front.get("lean"):
            record["lean_declaration"] = front["lean"]
        found[node_id] = (record, body.lstrip())
    return found


def _source_id(source: dict, index: int) -> str:
    for key in ("id", "citation_key", "key"):
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    seed = source.get("title") or source.get("file") or source.get("url") or f"source-{index + 1}"
    return slugify(str(seed))


def _normalize_sources(metadata: dict) -> dict[str, str]:
    sources = metadata.setdefault("sources", [])
    if not isinstance(sources, list):
        raise WikiError("metadata.sources must be a list")
    aliases: dict[str, str] = {}
    seen: set[str] = set()
    for index, source in enumerate(sources):
        if isinstance(source, str):
            source = {"file": source}
            sources[index] = source
        if not isinstance(source, dict):
            raise WikiError(f"metadata.sources[{index}] must be a string or object")
        source_id = _source_id(source, index)
        if source_id in seen:
            raise WikiError(f"duplicate source id {source_id!r}")
        seen.add(source_id)
        source["id"] = source_id
        for key in ("file", "title", "url", "citation_key", "key"):
            value = source.get(key)
            if isinstance(value, str) and value:
                aliases[value] = source_id
        aliases[source_id] = source_id
    return aliases


def _normalize_refs(node: dict, aliases: dict[str, str]) -> None:
    raw_refs = node.get("source_refs") or []
    if not isinstance(raw_refs, list):
        raise WikiError(f"node {node.get('id')!r} source_refs must be a list")
    normalized: list[dict[str, Any]] = []
    for raw in raw_refs:
        if isinstance(raw, str):
            normalized.append({"source": aliases[raw]} if raw in aliases else {"locator": raw})
            continue
        if not isinstance(raw, dict):
            raise WikiError(f"node {node.get('id')!r} has an invalid source_ref")
        ref = dict(raw)
        source = next(
            (ref.get(key) for key in ("source", "id", "file", "citation_key") if ref.get(key)),
            None,
        )
        if isinstance(source, str):
            ref["source"] = aliases.get(source, source)
        locator = next(
            (ref.get(key) for key in ("locator", "location", "where", "pages", "page") if ref.get(key)),
            None,
        )
        if locator is not None:
            ref["locator"] = str(locator)
        ref.setdefault("role", "statement")
        normalized.append(ref)
    node["source_refs"] = normalized


def _normalize_edges(node: dict) -> None:
    legacy = node.get("depends_on") or []
    if not isinstance(legacy, list) or not all(isinstance(item, str) for item in legacy):
        raise WikiError(f"node {node.get('id')!r} depends_on must be a list of strings")
    if not any(field in node for field in DEPENDENCY_FIELDS):
        node["statement_depends_on"] = []
        node["proof_depends_on"] = list(legacy)
    for field in DEPENDENCY_FIELDS:
        values = node.setdefault(field, [])
        if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
            raise WikiError(f"node {node.get('id')!r} {field} must be a list of strings")
        node[field] = list(dict.fromkeys(values))
    node["depends_on"] = list(dict.fromkeys(node["statement_depends_on"] + node["proof_depends_on"]))
    related = node.setdefault("related", [])
    if not isinstance(related, list) or not all(isinstance(item, str) for item in related):
        raise WikiError(f"node {node.get('id')!r} related must be a list of strings")


def migrate(project: Path, *, import_blueprint: bool = True) -> dict[str, int]:
    project = project.resolve()
    graph_path = project / "graph.json"
    graph = _load_graph(graph_path)
    _snapshot_migration(project)
    wiki = ensure_layout(project)
    source_pages = _import_blueprint_sources(project, wiki, graph["metadata"]) if import_blueprint else 0
    nodes: dict[str, dict] = graph["nodes"]
    imported = 0
    if import_blueprint:
        for node_id, (record, body) in _legacy_blueprint_nodes(project).items():
            if node_id in nodes:
                continue
            nodes[node_id] = record
            if not body:
                imported += 1
                continue
            destination = wiki / "nodes" / f"{slugify(node_id)}.md"
            if destination.exists():
                raise WikiError(f"refusing to overwrite authored page: {destination}")
            destination.write_text(body, encoding="utf-8")
            record["content"] = destination.relative_to(project).as_posix()
            imported += 1

    aliases = _normalize_sources(graph["metadata"])
    moved = 0
    for node_id in sorted(nodes):
        node = nodes[node_id]
        if not isinstance(node, dict):
            raise WikiError(f"node {node_id!r} must be an object")
        record_id = node.setdefault("id", node_id)
        if record_id != node_id:
            raise WikiError(f"node key {node_id!r} does not match record id {record_id!r}")
        _normalize_edges(node)
        _normalize_refs(node, aliases)
        content = node.get("content")
        if not isinstance(content, str) or not content:
            continue
        source = _safe_path(project, content, label=f"node {node_id!r} content")
        if source.parent != project / "informal_content":
            continue
        destination = wiki / "nodes" / f"{slugify(node_id)}.md"
        if destination.exists() and destination.resolve() != source.resolve():
            raise WikiError(f"refusing to overwrite authored page: {destination}")
        if source.is_file():
            shutil.move(source, destination)
            moved += 1
        node["content"] = destination.relative_to(project).as_posix()
    informal = project / "informal_content"
    if informal.is_dir() and not any(informal.iterdir()):
        informal.rmdir()
    migrate_graph(graph)
    _write_json_atomic(graph_path, graph)
    return {"nodes": len(nodes), "moved": moved, "imported": imported, "sources": source_pages}


def _md_label(value: Any) -> str:
    return str(value).replace("[", "\\[").replace("]", "\\]").replace("\n", " ")


def _source_map(metadata: dict) -> dict[str, dict]:
    return {
        source["id"]: source
        for source in metadata.get("sources", [])
        if isinstance(source, dict) and isinstance(source.get("id"), str)
    }


def _link(
    target: str,
    labels: dict[str, str],
    names: dict[str, str],
    *,
    prefix: str = "",
    folder: str = "cells",
) -> str:
    label = _md_label(names.get(target, target))
    if target not in labels:
        return f"`{label}` (unresolved)"
    return f"[{label}]({prefix}{folder}/{labels[target]}.md)"


def _edge_annotation(edge: dict[str, Any]) -> str:
    confidence = edge.get("confidence", "unknown")
    provenance = edge.get("provenance") or {}
    origin = provenance.get("source") or provenance.get("kind")
    locator = provenance.get("locator")
    details = [f"confidence: {confidence}"]
    if origin:
        details.append(f"provenance: {origin}")
    if locator:
        details.append(f"locator: {locator}")
    if edge.get("evidence"):
        details.append("evidence attached")
    return "; ".join(_md_label(item) for item in details)


def _authored_mathematics(project: Path, node_id: str, node: dict[str, Any]) -> str | None:
    content = node.get("content")
    if not isinstance(content, str) or not content:
        return None
    path = _safe_path(project, content, label=f"node {node_id!r} content")
    if not path.is_file() or path.is_symlink():
        return None
    body = path.read_text(encoding="utf-8").strip()
    # The cell page already owns the H1. Preserve the authored body without a
    # duplicate top-level title while retaining all lower-level structure.
    body = re.sub(r"^#\s+[^\n]+\n*", "", body, count=1).strip()
    return body or None


def _review_summary(project: Path, node_id: str) -> str:
    path = project / "review_status.json"
    if not path.is_file():
        return "not reviewed"
    try:
        review = json.loads(path.read_text(encoding="utf-8")).get("reviews", {}).get(node_id, {})
    except (OSError, json.JSONDecodeError, AttributeError):
        return "review data unreadable"
    human = review.get("human") if isinstance(review, dict) else None
    ai = review.get("ai") if isinstance(review, dict) else None
    verdict = (human or {}).get("verdict") or (ai or {}).get("verdict")
    return str(verdict or "not reviewed")


def _graph_revision(graph: dict, project: Path) -> str:
    durable = json.loads(json.dumps(graph))
    metadata = durable.get("metadata", {})
    for key in ("lean_root", "created_at", "last_updated"):
        metadata.pop(key, None)
    content_hashes: dict[str, str] = {}
    for node_id, node in sorted(graph["nodes"].items()):
        content = node.get("content")
        if not isinstance(content, str):
            continue
        path = _safe_path(project, content, label=f"node {node_id!r} content")
        if path.is_file():
            content_hashes[node_id] = hashlib.sha256(path.read_bytes()).hexdigest()
    payload = json.dumps(
        {"graph": durable, "content": content_hashes},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _git_commit(project: Path) -> str | None:
    try:
        return subprocess.run(
            ["git", "-C", str(project), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def render(project: Path) -> dict[str, str]:
    project = project.resolve()
    graph = _load_graph(project / "graph.json")
    nodes: dict[str, dict] = graph["nodes"]
    edges = normalize_edges(
        graph, migrate=graph.get("version", 0) < SCHEMA_VERSION and "edges" not in graph
    )
    labels = _slug_map(list(nodes))
    names = {node_id: str(node.get("name") or node.get("title") or node_id) for node_id, node in nodes.items()}
    dependents: dict[str, list[str]] = {node_id: [] for node_id in nodes}
    children: dict[str, list[str]] = {node_id: [] for node_id in nodes}
    outgoing: dict[str, list[dict[str, Any]]] = {node_id: [] for node_id in nodes}
    for edge in edges:
        outgoing[edge["source"]].append(edge)
        if edge["kind"] in ("statement-requires", "proof-requires"):
            dependents[edge["target"]].append(edge["source"])
    for node_id, node in nodes.items():
        parent = node.get("parent")
        if parent in children:
            children[parent].append(node_id)

    files: dict[str, str] = {}
    revision = _graph_revision(graph, project)
    targets = graph.get("metadata", {}).get("targets", [])
    target_ids = [entry if isinstance(entry, str) else entry.get("node") for entry in targets]
    target_ids = [item for item in target_ids if isinstance(item, str)]
    index = [
        "# Generated Cell Blueprint",
        "",
        "> Generated from `graph.json` and authored wiki pages. Do not edit this directory.",
        "",
        f"Graph revision: `{revision}`",
        "",
        f"Cells: **{len(nodes)}**",
        "",
        "## Mission targets",
        "",
    ]
    index.extend(
        f"- [{_md_label(names.get(node_id, node_id))}](targets/{labels[node_id]}.md)"
        for node_id in target_ids
        if node_id in labels
    )
    if not target_ids:
        index.append("- None declared")
    index += ["", "## Tiers", ""]
    tiers = sorted({node.get("tier") for node in nodes.values()}, key=lambda value: (value is None, str(value)))
    for tier in tiers:
        index.append(f"### Tier {tier}")
        index.append("")
        for node_id in sorted(
            (item for item, node in nodes.items() if node.get("tier") == tier),
            key=lambda item: (names[item].lower(), item),
        ):
            folder = "supercells" if tier == 1 else "cells"
            index.append(f"- {_link(node_id, labels, names, folder=folder)}")
        index.append("")
    files["index.md"] = "\n".join(index).rstrip() + "\n"

    sources = _source_map(graph.get("metadata", {}))
    for node_id in sorted(nodes):
        node = nodes[node_id]
        page = [f"# {_md_label(names[node_id])}", "", f"Stable ID: `{node_id}`", ""]
        page += [
            "## State",
            "",
            f"- Tier: `{node.get('tier')}`",
            f"- Kind: `{node.get('kind', 'unknown')}`",
            f"- Mathlib: `{node.get('mathlib_status', 'unknown')}`",
            f"- Review: `{_review_summary(project, node_id)}`",
            f"- Kernel evidence: `{'present' if (project / 'kernel' / f'{node_id}.txt').is_file() else 'absent'}`",
            "",
        ]
        parent = node.get("parent")
        if isinstance(parent, str):
            folder = "supercells" if nodes.get(parent, {}).get("tier") == 1 else "cells"
            page += [
                "## Parent",
                "",
                f"- {_link(parent, labels, names, prefix='../', folder=folder)}",
                "",
            ]
        for title, field in (
            ("Statement prerequisites", "statement_depends_on"),
            ("Proof prerequisites", "proof_depends_on"),
        ):
            page += [f"## {title}", ""]
            kind = "statement-requires" if field == "statement_depends_on" else "proof-requires"
            typed_edges = [edge for edge in outgoing[node_id] if edge["kind"] == kind]
            page.extend(
                f"- {_link(edge['target'], labels, names, prefix='../')} ({_edge_annotation(edge)})"
                for edge in typed_edges
            )
            if not typed_edges:
                page.append("- None")
            page.append("")
        page += ["## Dependents", ""]
        page.extend(f"- {_link(item, labels, names, prefix='../')}" for item in sorted(set(dependents[node_id])))
        if not dependents[node_id]:
            page.append("- None")
        page += ["", "## Children", ""]
        page.extend(f"- {_link(item, labels, names, prefix='../')}" for item in sorted(children[node_id]))
        if not children[node_id]:
            page.append("- None")
        page += ["", "## Sources", ""]
        refs = node.get("source_refs") or []
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            source_id = ref.get("source")
            source = sources.get(source_id, {}) if isinstance(source_id, str) else {}
            label = source.get("title") or source_id or "Unregistered source"
            url = source.get("url")
            rendered = f"[{_md_label(label)}]({url})" if isinstance(url, str) else f"`{_md_label(label)}`"
            locator = ref.get("locator")
            role = ref.get("role")
            suffix = ", ".join(str(item) for item in (locator, role) if item)
            page.append(f"- {rendered}" + (f" ({suffix})" if suffix else ""))
        if not refs:
            page.append("- None")
        page += ["", "## Attached organs", ""]
        content = node.get("content")
        if isinstance(content, str):
            page.append(f"- Wiki body: `{_md_label(content)}`")
        if node.get("lean_declaration"):
            page.append(f"- Project Lean declaration: `{_md_label(node['lean_declaration'])}`")
        for declaration in node.get("mathlib_declarations") or []:
            page.append(f"- Mathlib declaration: `{_md_label(declaration)}`")
        if (project / "kernel" / f"{node_id}.txt").is_file():
            page.append(f"- Kernel evidence: `kernel/{_md_label(node_id)}.txt`")
        if len(page) >= 2 and page[-2:] == ["## Attached organs", ""]:
            page.append("- None")
        aliases = node.get("aliases") or []
        page += ["", "## Aliases", ""]
        page.extend(f"- `{_md_label(alias)}`" for alias in aliases)
        if not aliases:
            page.append("- None")
        soft_edges = [edge for edge in outgoing[node_id] if edge["kind"] not in ("statement-requires", "proof-requires")]
        page += ["", "## Related cells", ""]
        page.extend(
            f"- `{edge['kind']}`: {_link(edge['target'], labels, names, prefix='../')} "
            f"({_edge_annotation(edge)})"
            for edge in soft_edges
        )
        if not soft_edges:
            page.append("- None")
        authored = _authored_mathematics(project, node_id, node)
        page += ["", "---", "", "## Authored mathematics", ""]
        page.append(authored or "_No authored mathematical body is attached._")
        files[f"cells/{labels[node_id]}.md"] = "\n".join(page).rstrip() + "\n"

    for cluster_id in sorted(
        (node_id for node_id, node in nodes.items() if node.get("tier") == 1),
        key=lambda item: (names[item].lower(), item),
    ):
        cluster = nodes[cluster_id]
        page = [
            f"# {_md_label(names[cluster_id])}",
            "",
            f"Graph node: {_link(cluster_id, labels, names, prefix='../')}",
            "",
            "## Members",
            "",
        ]
        members = sorted(children[cluster_id], key=lambda item: (names[item].lower(), item))
        page.extend(f"- {_link(item, labels, names, prefix='../')}" for item in members)
        if not members:
            page.append("- None")
        page += ["", "## Cluster prerequisites", ""]
        dependencies = cluster.get("depends_on") or []
        page.extend(f"- {_link(item, labels, names, prefix='../')}" for item in dependencies)
        if not dependencies:
            page.append("- None")
        files[f"supercells/{labels[cluster_id]}.md"] = "\n".join(page).rstrip() + "\n"

    def dependency_cone(target: str) -> set[str]:
        seen: set[str] = set()
        stack = [target]
        while stack:
            current = stack.pop()
            if current in seen or current not in nodes:
                continue
            seen.add(current)
            stack.extend(nodes[current].get("depends_on") or [])
        return seen

    for target_id in sorted(target_ids):
        if target_id not in nodes:
            continue
        cone = dependency_cone(target_id)
        page = [
            f"# Target: {_md_label(names[target_id])}",
            "",
            f"Target node: {_link(target_id, labels, names, prefix='../')}",
            "",
            "## Prerequisite cone",
            "",
        ]
        for node_id in sorted(cone, key=lambda item: (nodes[item].get("tier", 0), names[item].lower(), item)):
            marker = " (target)" if node_id == target_id else ""
            page.append(f"- {_link(node_id, labels, names, prefix='../')}{marker}")
        files[f"targets/{labels[target_id]}.md"] = "\n".join(page).rstrip() + "\n"

    source_index = ["# Source Index", ""]
    for source_id in sorted(sources):
        source = sources[source_id]
        title = source.get("title") or source_id
        url = source.get("url")
        wiki_path = source.get("wiki")
        rendered = f"[{_md_label(title)}]({url})" if isinstance(url, str) else f"**{_md_label(title)}**"
        source_index.append(f"- `{source_id}`: {rendered}")
        if isinstance(wiki_path, str):
            source_index.append(f"  - Notes: [source page](../../{wiki_path})")
    if not sources:
        source_index.append("- None registered")
    files["sources.md"] = "\n".join(source_index) + "\n"
    files["aliases.json"] = json.dumps(
        {"schema": "autoform-aliases/v1", "aliases": alias_index(nodes)},
        indent=2,
        sort_keys=True,
    ) + "\n"
    manifest = {
        "schema": "autoform-wiki/v2",
        "graph_revision": revision,
        "git_commit": _git_commit(project),
        "generated_files": sorted([*files, "manifest.json"]),
    }
    files["manifest.json"] = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    return files


def _cell_organs(project: Path, node_id: str, node: dict[str, Any]) -> list[dict[str, Any]]:
    organs: list[dict[str, Any]] = []
    if isinstance(node.get("content"), str):
        organs.append({"kind": "wiki", "ref": node["content"]})
    for ref in node.get("source_refs") or []:
        if isinstance(ref, dict):
            organs.append({"kind": "source", **ref})
    if node.get("lean_declaration"):
        organ = {"kind": "lean", "ref": node["lean_declaration"]}
        if node.get("lean_file"):
            organ["path"] = node["lean_file"]
        organs.append(organ)
    for declaration in node.get("mathlib_declarations") or []:
        organs.append({"kind": "mathlib", "ref": declaration})
    kernel = project / "kernel" / f"{node_id}.txt"
    if kernel.is_file():
        organs.append({"kind": "kernel", "ref": f"kernel/{node_id}.txt"})
    review_path = project / "review_status.json"
    if review_path.is_file():
        try:
            reviews = json.loads(review_path.read_text(encoding="utf-8")).get("reviews", {})
        except (OSError, json.JSONDecodeError, AttributeError):
            reviews = {}
        if isinstance(reviews, dict) and reviews.get(node_id):
            organs.append({"kind": "review", "ref": f"review_status.json#reviews.{node_id}"})
    return organs


def query_cell(project: Path, key: str, *, depth: int = 0) -> dict[str, Any]:
    """Return one cell and an optional local graph neighborhood as JSON data."""
    project = project.resolve()
    graph = _load_graph(project / "graph.json")
    nodes: dict[str, dict] = graph["nodes"]
    edges = normalize_edges(
        graph, migrate=graph.get("version", 0) < SCHEMA_VERSION and "edges" not in graph
    )
    node_id = resolve_alias(nodes, key)
    selected = {node_id}
    frontier = {node_id}
    for _ in range(max(depth, 0)):
        adjacent = {
            endpoint
            for edge in edges
            if edge["source"] in frontier or edge["target"] in frontier
            for endpoint in (edge["source"], edge["target"])
        }
        for candidate, node in nodes.items():
            if candidate in frontier and isinstance(node.get("parent"), str):
                adjacent.add(node["parent"])
            if node.get("parent") in frontier:
                adjacent.add(candidate)
        frontier = adjacent - selected
        selected.update(adjacent)
    cells = {}
    for selected_id in sorted(selected):
        node = dict(nodes[selected_id])
        node["organs"] = _cell_organs(project, selected_id, node)
        cells[selected_id] = node
    return {
        "schema": "autoform-cell-query/v1",
        "focus": node_id,
        "cells": cells,
        "edges": [
            edge for edge in edges if edge["source"] in selected and edge["target"] in selected
        ],
    }


def search_cells(project: Path, text: str) -> list[dict[str, Any]]:
    graph = _load_graph(project.resolve() / "graph.json")
    needle = text.casefold()
    matched: set[str] = set()
    for alias, node_ids in alias_index(graph["nodes"]).items():
        if needle in alias.casefold():
            matched.update(node_ids)
    for node_id, node in graph["nodes"].items():
        values = [node.get("description")]
        haystack = "\n".join(value for value in values if isinstance(value, str)).casefold()
        if needle in haystack:
            matched.add(node_id)
    results = [
        {
            "id": node_id,
            "name": graph["nodes"][node_id].get("name") or graph["nodes"][node_id].get("title") or node_id,
        }
        for node_id in matched
    ]
    return sorted(results, key=lambda result: (str(result["name"]).casefold(), result["id"]))


def build(project: Path, *, check: bool = False) -> bool:
    wiki = ensure_layout(project)
    output = wiki / "_generated"
    expected = render(project)
    if check:
        actual = (
            {
                path.relative_to(output).as_posix(): path.read_text(encoding="utf-8")
                for path in sorted(output.rglob("*"))
                if path.is_file()
            }
            if output.is_dir()
            else {}
        )
        return actual == expected
    stage = Path(tempfile.mkdtemp(prefix=".wiki-generated-", dir=wiki))
    try:
        for relative, text in expected.items():
            destination = _safe_path(stage, relative, label="generated wiki path")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(text, encoding="utf-8")
        if output.exists():
            if output.is_symlink() or not output.is_dir():
                raise WikiError(f"refusing to replace non-directory at {output}")
            shutil.rmtree(output)
        os.replace(stage, output)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path, help="directory containing graph.json")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init", help="create missing authored wiki directories")
    migrate_parser = subparsers.add_parser("migrate", help="migrate legacy prose and graph schema")
    migrate_parser.add_argument("--no-blueprint-import", action="store_true")
    subparsers.add_parser("build", help="rebuild wiki/_generated")
    subparsers.add_parser("check", help="verify wiki/_generated is current")
    cell_parser = subparsers.add_parser("cell", help="resolve a cell id or alias and print JSON")
    cell_parser.add_argument("key")
    neighborhood_parser = subparsers.add_parser("neighborhood", help="print a cell and its local DAG neighborhood")
    neighborhood_parser.add_argument("key")
    neighborhood_parser.add_argument("--depth", type=int, default=1)
    search_parser = subparsers.add_parser("search", help="search cell ids, names, descriptions, and aliases")
    search_parser.add_argument("text")
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            ensure_layout(args.project)
            print(f"wiki initialized: {(args.project / 'wiki').resolve()}")
        elif args.command == "migrate":
            result = migrate(args.project, import_blueprint=not args.no_blueprint_import)
            print(
                f"wiki migrated: {result['nodes']} nodes, {result['moved']} prose files moved, "
                f"{result['imported']} blueprint nodes and {result['sources']} sources imported"
            )
        elif args.command == "build":
            build(args.project)
            print(f"wiki generated: {(args.project / 'wiki' / '_generated').resolve()}")
        elif args.command == "cell":
            print(json.dumps(query_cell(args.project, args.key), indent=2, sort_keys=True))
        elif args.command == "neighborhood":
            print(json.dumps(query_cell(args.project, args.key, depth=args.depth), indent=2, sort_keys=True))
        elif args.command == "search":
            print(json.dumps(search_cells(args.project, args.text), indent=2, sort_keys=True))
        elif not build(args.project, check=True):
            print("wiki is stale; run the build command", file=sys.stderr)
            return 1
    except (OSError, ValueError, WikiError) as error:
        print(f"wiki operation failed: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
