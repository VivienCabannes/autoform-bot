#!/usr/bin/env python3
"""Export a deterministic, read-only Autoform dashboard for GitHub Pages."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


SCRIPT = Path(__file__).resolve()
REPO_ROOT = SCRIPT.parents[1]
REVIEW_UI = REPO_ROOT / "scripts" / "review_ui"
for directory in (REPO_ROOT / "scripts", REVIEW_UI):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

import export_blueprint as eb  # noqa: E402
import review_model as rm  # noqa: E402


SCHEMA_VERSION = 1
PUBLISHED_CATEGORIES = (
    "graph structure",
    "theorem content",
    "proof status",
    "review verdicts",
    "kernel evidence",
)
EXCLUDED_CATEGORIES = (
    "agent activity",
    "task queues",
    "dispatcher logs",
    "backend configuration",
    "credentials",
    "local filesystem paths",
)
OPERATIONAL_FILENAMES = frozenset(
    {
        "agents_status.json",
        "task_queue.json",
        "dispatch.log",
        "formalization.yaml",
    }
)
ASSET_NAMES = ("review.css", "static_dashboard.css", "static_dashboard.js")
BRAND_ASSET = REPO_ROOT / "assets" / "autoform-small.svg"
_INCOMPLETE = re.compile(r"\b(?:sorry|admit|sorryAx)\b")


class ExportError(RuntimeError):
    """A publication-safety or input-contract failure."""


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _pretty_json(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def _safe_path(root: Path, candidate: Path | str, *, label: str) -> Path:
    root = root.resolve()
    candidate = Path(candidate)
    target = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    try:
        target.relative_to(root)
    except ValueError as error:
        raise ExportError(f"{label} must stay inside {root}") from error
    return target


def _read_json_object(path: Path, *, missing: dict | None = None) -> dict:
    if not path.is_file():
        return dict(missing or {})
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ExportError(f"cannot read valid JSON from {path.name}: {error}") from error
    if not isinstance(value, dict):
        raise ExportError(f"{path.name} must contain a JSON object")
    return value


def _sanitize_sidecar(raw: dict, node_ids: set[str]) -> dict:
    """Keep only scores and verdicts needed by the read-only review model."""
    reviews: dict[str, dict] = {}
    raw_reviews = raw.get("reviews") if isinstance(raw.get("reviews"), dict) else {}
    for node_id in sorted(node_ids):
        source = raw_reviews.get(node_id)
        if not isinstance(source, dict):
            continue
        clean: dict[str, dict] = {}
        ai = source.get("ai")
        if isinstance(ai, dict):
            clean_ai = {
                axis: ai[axis]
                for axis in rm.AXES
                if isinstance(ai.get(axis), (int, float)) and not isinstance(ai.get(axis), bool)
            }
            if ai.get("verdict") in rm.VERDICTS:
                clean_ai["verdict"] = ai["verdict"]
            if clean_ai:
                clean["ai"] = clean_ai
        human = source.get("human")
        if isinstance(human, dict) and human.get("verdict") in rm.VERDICTS:
            clean_human: dict[str, Any] = {"verdict": human["verdict"]}
            score = human.get("score")
            if isinstance(score, (int, float)) and not isinstance(score, bool):
                clean_human["score"] = score
            clean["human"] = clean_human
        if clean:
            reviews[node_id] = clean
    return {"version": 1, "settings": {"dial": "on-demand"}, "reviews": reviews}


def _safe_node_file(project_root: Path, node_id: str, node: dict, kind: str) -> Path | None:
    if kind == "content":
        candidates: list[Path | str] = []
        if isinstance(node.get("content"), str):
            candidates.append(node["content"])
        candidates.append(Path("informal_content") / f"{node_id}.md")
    else:
        candidates = [Path("kernel") / f"{node_id}.txt"]
    for candidate in candidates:
        try:
            path = _safe_path(project_root, candidate, label=f"node {node_id!r} {kind}")
        except ExportError:
            continue
        if path.is_file():
            return path
    return None


def _read_node_artifacts(project_root: Path, nodes: dict[str, dict]) -> tuple[dict[str, str], dict[str, str]]:
    content: dict[str, str] = {}
    kernel: dict[str, str] = {}
    for node_id in sorted(nodes):
        node = nodes[node_id]
        content_path = _safe_node_file(project_root, node_id, node, "content")
        kernel_path = _safe_node_file(project_root, node_id, node, "kernel")
        content[node_id] = (
            content_path.read_text(encoding="utf-8", errors="replace")
            if content_path is not None
            else ""
        )
        kernel[node_id] = (
            kernel_path.read_text(encoding="utf-8", errors="replace")
            if kernel_path is not None
            else ""
        )
    return content, kernel


def _incomplete_nodes(repo_root: Path, nodes: dict[str, dict]) -> set[str]:
    """Inspect only graph-pinned Lean files; never follow a path outside the repository."""
    incomplete: set[str] = set()
    for node_id in sorted(nodes):
        lean_file = nodes[node_id].get("lean_file")
        if not isinstance(lean_file, str) or not lean_file:
            continue
        try:
            path = _safe_path(repo_root, lean_file, label=f"node {node_id!r} lean_file")
        except ExportError:
            continue
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        code = "\n".join(line.split("--", 1)[0] for line in text.splitlines())
        if _INCOMPLETE.search(code):
            incomplete.add(node_id)

    result: set[str] = set()
    for node_id in incomplete:
        current: str | None = node_id
        seen: set[str] = set()
        while current and current in nodes and current not in seen:
            seen.add(current)
            result.add(current)
            parent = nodes[current].get("parent")
            current = parent if isinstance(parent, str) else None
    return result


def _proof_status(node: dict, *, incomplete: bool, kernel: str) -> str:
    if incomplete:
        return "incomplete"
    if rm.is_in_mathlib(node):
        return "reused-from-mathlib"
    if kernel.strip():
        return "kernel-evidence-recorded"
    return "unverified"


def _public_nodes(
    nodes: dict[str, dict],
    sidecar: dict,
    slugs: dict[str, str],
    incomplete: set[str],
    kernel: dict[str, str],
) -> list[dict]:
    published = []
    for node_id in sorted(nodes):
        node = nodes[node_id]
        parent = node.get("parent") if node.get("parent") in nodes else None
        dependencies = sorted(
            dep for dep in (node.get("depends_on") or []) if isinstance(dep, str) and dep in nodes
        )
        scorecard = rm.node_scorecard(node_id, sidecar)
        ai = scorecard.get("ai") or {}
        public_ai = {
            axis: ai.get(axis)
            for axis in rm.AXES
            if ai.get(axis) is not None
        }
        if ai.get("weighted") is not None:
            public_ai["weighted"] = ai["weighted"]
        if ai.get("verdict") in rm.VERDICTS:
            public_ai["verdict"] = ai["verdict"]
        public_human = scorecard.get("human") or {}
        review = {
            "effective": scorecard["effective"],
            "source": scorecard["source"],
            "ai": public_ai or None,
            "human": (
                {
                    key: public_human[key]
                    for key in ("verdict", "score")
                    if public_human.get(key) is not None
                }
                or None
            ),
        }
        published.append(
            {
                "id": node_id,
                "slug": slugs[node_id],
                "name": str(node.get("name") or node_id),
                "kind": str(node.get("kind") or "theorem"),
                "tier": eb.node_tier(node),
                "parent": parent,
                "depends_on": dependencies,
                "mathlib_status": str(node.get("mathlib_status") or "missing"),
                "mathlib_declarations": sorted(
                    str(item)
                    for item in (node.get("mathlib_declarations") or [])
                    if isinstance(item, str)
                ),
                "proof_status": _proof_status(
                    node,
                    incomplete=node_id in incomplete,
                    kernel=kernel[node_id],
                ),
                "review": review,
                "path": (
                    f"clusters/{slugs[node_id]}/"
                    if eb.node_tier(node) == 1
                    else f"nodes/{slugs[node_id]}/"
                ),
            }
        )
    return published


def build_snapshot(graph_path: Path, repo_root: Path, git_commit: str) -> tuple[dict, dict[str, str], dict[str, str]]:
    repo_root = repo_root.resolve()
    graph_path = _safe_path(repo_root, graph_path, label="graph")
    if not graph_path.is_file():
        raise ExportError(f"graph file not found: {graph_path}")
    nodes, metadata = rm.load_graph(graph_path)
    project_root = graph_path.parent
    raw_sidecar = _read_json_object(project_root / "review_status.json", missing=rm.empty_sidecar())
    sidecar = _sanitize_sidecar(raw_sidecar, set(nodes))
    content, kernel = _read_node_artifacts(project_root, nodes)
    incomplete = _incomplete_nodes(repo_root, nodes)
    slugs = eb.build_slug_map(nodes)
    public_nodes = _public_nodes(nodes, sidecar, slugs, incomplete, kernel)
    computed = rm.compute_state(nodes, sidecar, incomplete)
    computed.pop("dial", None)

    by_id = {node["id"]: node for node in public_nodes}
    clusters = []
    for node in public_nodes:
        if node["tier"] != 1:
            continue
        members = sorted(
            child["id"] for child in public_nodes if child.get("parent") == node["id"]
        )
        clusters.append(
            {
                "id": node["id"],
                "slug": node["slug"],
                "name": node["name"],
                "members": members,
                "path": node["path"],
                "review": node["review"],
            }
        )

    revision_input = {
        "title": str(metadata.get("title") or "Autoform dashboard"),
        "nodes": public_nodes,
        "content": {node_id: content[node_id] for node_id in sorted(content)},
        "kernel": {node_id: kernel[node_id] for node_id in sorted(kernel)},
        "computed": computed,
    }
    graph_revision = hashlib.sha256(_canonical_bytes(revision_input)).hexdigest()
    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "project": {"title": revision_input["title"]},
        "publication": {
            "git_commit": git_commit,
            "graph_revision": graph_revision,
            "published_categories": list(PUBLISHED_CATEGORIES),
        },
        "tiers": sorted({node["tier"] for node in public_nodes}),
        "nodes": public_nodes,
        "clusters": clusters,
        "coverage": computed["coverage"],
        "trust_frontier": computed["trust_frontier"],
        "tainted": computed["tainted"],
        "sorry": computed["sorry"],
        "palette": rm.PALETTE,
    }
    if set(by_id) != set(nodes):
        raise ExportError("published node set does not match graph")
    return snapshot, content, kernel


def _render_markdown(markdown: str) -> str:
    paragraphs: list[str] = []
    pending: list[str] = []

    def flush() -> None:
        if pending:
            paragraphs.append("<p>" + "<br>".join(html.escape(line) for line in pending) + "</p>")
            pending.clear()

    for line in markdown.strip().splitlines():
        stripped = line.strip()
        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if not stripped:
            flush()
        elif heading:
            flush()
            level = min(len(heading.group(1)) + 1, 6)
            paragraphs.append(f"<h{level}>{html.escape(heading.group(2).strip())}</h{level}>")
        else:
            pending.append(stripped)
    flush()
    return "\n".join(paragraphs) or "<p><em>No theorem content was committed.</em></p>"


def _page(title: str, body: str, *, depth: int, mathjax: bool = False, state_url: str | None = None) -> bytes:
    prefix = "../" * depth
    scripts = ""
    if mathjax:
        scripts += (
            "<script>window.MathJax={tex:{inlineMath:[['$','$'],['\\\\(','\\\\)']],"
            "displayMath:[['$$','$$'],['\\\\[','\\\\]']]},"
            "options:{skipHtmlTags:['script','noscript','style','textarea','pre','code']}};</script>"
            "<script async src='https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js'></script>"
        )
    if state_url is not None:
        scripts += f"<script>window.AUTOFORM_STATE_URL={json.dumps(state_url)};</script>"
        scripts += f"<script defer src='{prefix}assets/static_dashboard.js'></script>"
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title>"
        f"<link rel='icon' type='image/svg+xml' href='{prefix}assets/{BRAND_ASSET.name}'>"
        f"<link rel='stylesheet' href='{prefix}assets/review.css'>"
        f"<link rel='stylesheet' href='{prefix}assets/static_dashboard.css'>"
        f"{scripts}</head><body class='af-static'>"
        f"<header class='af-site-header'><a class='af-brand' href='{prefix}' "
        "aria-label='Autoform dashboard'>"
        f"<img class='af-brand-mark' src='{prefix}assets/{BRAND_ASSET.name}' "
        "width='24' height='24' alt=''><span>Autoform</span></a>"
        f"<span class='af-site-title'>{html.escape(title)}</span>"
        "<strong>read-only snapshot</strong></header>"
        f"<main class='af-site-main'>{body}</main></body></html>"
    ).encode("utf-8")


def _status_badge(node: dict) -> str:
    state = node["review"]["effective"]
    return (
        f"<span class='af-badge af-{html.escape(state)}'>{html.escape(state)}</span>"
        f"<span class='af-proof'>{html.escape(node['proof_status'])}</span>"
    )


def _node_href(node_id: str, node_by_id: dict[str, dict], *, depth: int) -> str:
    target = node_by_id[node_id]["path"]
    return "../" * depth + target


def _render_index(snapshot: dict, blueprint: bool = False) -> bytes:
    node_by_id = {node["id"]: node for node in snapshot["nodes"]}
    coverage = snapshot["coverage"]
    frontier = snapshot["trust_frontier"]
    frontier_html = (
        "".join(
            f"<li><a href='{html.escape(node_by_id[node_id]['path'])}'>{html.escape(node_id)}</a></li>"
            for node_id in frontier
        )
        if frontier
        else "<li><em>No trusted sink yet.</em></li>"
    )
    cards = []
    for node in snapshot["nodes"]:
        deps = " ".join(
            f"<a href='{html.escape(node_by_id[dep]['path'])}'>{html.escape(dep)}</a>"
            for dep in node["depends_on"]
        ) or "<em>none</em>"
        cards.append(
            f"<article class='af-node-card' data-tier='{node['tier']}' "
            f"data-search='{html.escape((node['id'] + ' ' + node['name']).lower())}'>"
            f"<div class='af-node-card-head'><a href='{html.escape(node['path'])}'>"
            f"{html.escape(node['name'])}</a>{_status_badge(node)}</div>"
            f"<code>{html.escape(node['id'])}</code>"
            f"<p>depends on: {deps}</p></article>"
        )
    tier_buttons = "".join(
        f"<button type='button' data-tier='{tier}'>{tier}</button>" for tier in snapshot["tiers"]
    )
    commit = snapshot["publication"]["git_commit"]
    revision = snapshot["publication"]["graph_revision"]
    body = (
        "<section class='af-summary'>"
        f"<div><span>coverage</span><strong>{coverage['reviewed']}/{coverage['total']}</strong></div>"
        f"<div><span>human confirmed</span><strong>{coverage['human_confirmed']}</strong></div>"
        f"<div><span>Git commit</span><code>{html.escape(commit[:12])}</code></div>"
        f"<div><span>graph revision</span><code>{html.escape(revision[:12])}</code></div>"
        "</section>"
        "<section class='af-frontier'><h2>Trust frontier</h2>"
        f"<ul>{frontier_html}</ul></section>"
        + ("<section class='af-blueprint'><h2>Blueprint</h2><p>"
           "<a href='blueprint/'>Read the informal blueprint</a> — the project's "
           "unified mathematical argument, with its dependency graph.</p></section>"
           if blueprint else "")
        + "<section class='af-graph-section'><div class='af-graph-tools'>"
        "<h2>Dependency graph</h2>"
        "<label>Filter <input id='af-filter' type='search' autocomplete='off'></label>"
        f"<div class='af-tier-filter'><button type='button' data-tier='all'>all</button>{tier_buttons}</div>"
        "</div><p class='af-static-note'>Select a node for theorem content, review status, and kernel evidence.</p>"
        f"<div id='af-graph' class='af-node-grid'>{''.join(cards)}</div></section>"
    )
    return _page(snapshot["project"]["title"], body, depth=0, state_url="data/state.json")


def _review_html(node: dict) -> str:
    review = node["review"]
    rows = []
    for axis in rm.AXES:
        value = (review.get("ai") or {}).get(axis)
        rows.append(f"<tr><td>{html.escape(axis)}</td><td>{html.escape(str(value if value is not None else '-'))}</td></tr>")
    return (
        "<section class='af-review'><h2>Review verdict</h2>"
        f"<p>{_status_badge(node)} source: {html.escape(str(review.get('source') or 'none'))}</p>"
        f"<table><tbody>{''.join(rows)}</tbody></table></section>"
    )


def _render_node_page(node: dict, node_by_id: dict[str, dict], content: str, kernel: str) -> bytes:
    dependencies = "".join(
        f"<li><a href='{html.escape(_node_href(dep, node_by_id, depth=2))}'>{html.escape(dep)}</a></li>"
        for dep in node["depends_on"]
    ) or "<li><em>none</em></li>"
    declarations = "".join(
        f"<li><code>{html.escape(item)}</code></li>" for item in node["mathlib_declarations"]
    ) or "<li><em>none recorded</em></li>"
    kernel_html = (
        f"<pre><code>{html.escape(kernel)}</code></pre>"
        if kernel.strip()
        else "<p><em>No kernel evidence was committed.</em></p>"
    )
    body = (
        f"<nav class='af-breadcrumb'><a href='../../'>graph</a> / {html.escape(node['id'])}</nav>"
        f"<article class='af-theorem'><h1>{html.escape(node['name'])}</h1>"
        f"<p><code>{html.escape(node['id'])}</code> {_status_badge(node)}</p>"
        f"<div class='af-content'>{_render_markdown(content)}</div></article>"
        "<div class='af-detail-grid'><section><h2>Dependencies</h2>"
        f"<ul>{dependencies}</ul><h2>Mathlib declarations</h2><ul>{declarations}</ul></section>"
        f"{_review_html(node)}</div>"
        f"<section class='af-kernel'><h2>Kernel evidence</h2>{kernel_html}</section>"
    )
    return _page(f"{node['name']} | Autoform", body, depth=2, mathjax=True)


def _render_cluster_page(cluster: dict, node_by_id: dict[str, dict]) -> bytes:
    members = "".join(
        f"<li><a href='{html.escape(_node_href(node_id, node_by_id, depth=2))}'>"
        f"{html.escape(node_by_id[node_id]['name'])}</a>{_status_badge(node_by_id[node_id])}</li>"
        for node_id in cluster["members"]
    ) or "<li><em>No committed child nodes.</em></li>"
    body = (
        "<nav class='af-breadcrumb'><a href='../../'>graph</a> / cluster</nav>"
        f"<section><h1>{html.escape(cluster['name'])}</h1>"
        f"<p><code>{html.escape(cluster['id'])}</code></p>"
        f"<ul class='af-member-list'>{members}</ul></section>"
    )
    return _page(f"{cluster['name']} | Autoform", body, depth=2)


def _write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _path_uses_symlink(path: Path, stop: Path) -> bool:
    current = path
    stop = stop.resolve()
    while current != current.parent:
        if current.is_symlink():
            return True
        if current == stop:
            return False
        current = current.parent
    return False


def export_site(graph_path: Path, output: Path, repo_root: Path, *, git_commit: str,
                blueprint: Path | None = None) -> Path:
    repo_root = repo_root.resolve()
    raw_output = output if output.is_absolute() else repo_root / output
    if _path_uses_symlink(raw_output, repo_root):
        raise ExportError("output path must not contain a symlink")
    output = _safe_path(repo_root, output, label="output")
    graph_path = _safe_path(repo_root, graph_path, label="graph")
    protected = {repo_root, graph_path, graph_path.parent}
    if output in protected:
        raise ExportError("output must be a dedicated directory separate from repository inputs")
    if any(output in path.parents for path in protected):
        raise ExportError("output must not contain the repository or dashboard inputs")
    if blueprint is not None:
        blueprint_path = _safe_path(repo_root, blueprint, label="blueprint")
        if output == blueprint_path or output in blueprint_path.parents or blueprint_path in output.parents:
            raise ExportError("output and blueprint directories must not overlap")
    snapshot, content, kernel = build_snapshot(graph_path, repo_root, git_commit)
    node_by_id = {node["id"]: node for node in snapshot["nodes"]}

    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
    try:
        for directory in ("nodes", "clusters", "assets", "data"):
            (stage / directory).mkdir()
        _write(stage / ".nojekyll", b"")
        _write(stage / "index.html", _render_index(snapshot, blueprint=blueprint is not None))
        _write(stage / "data" / "state.json", _pretty_json(snapshot))
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "git_commit": snapshot["publication"]["git_commit"],
            "graph_revision": snapshot["publication"]["graph_revision"],
            "published_categories": list(PUBLISHED_CATEGORIES),
        }
        _write(stage / "publication.json", _pretty_json(manifest))
        for node in snapshot["nodes"]:
            if node["tier"] == 1:
                continue
            _write(
                stage / "nodes" / node["slug"] / "index.html",
                _render_node_page(node, node_by_id, content[node["id"]], kernel[node["id"]]),
            )
        for cluster in snapshot["clusters"]:
            _write(
                stage / "clusters" / cluster["slug"] / "index.html",
                _render_cluster_page(cluster, node_by_id),
            )
        asset_root = REPO_ROOT / "assets" / "review"
        for name in ASSET_NAMES:
            source = asset_root / name
            if not source.is_file():
                raise ExportError(f"required dashboard asset is missing: {name}")
            _write(stage / "assets" / name, source.read_bytes())
        if not BRAND_ASSET.is_file():
            raise ExportError(f"required dashboard asset is missing: {BRAND_ASSET.name}")
        _write(stage / "assets" / BRAND_ASSET.name, BRAND_ASSET.read_bytes())

        # The blueprint is the SHARED informal argument — it belongs on the
        # published site next to the dashboard, not on one operator's laptop.
        # It is copied only when already built (the LaTeX toolchain is the
        # caller's business), and it is static HTML like everything else here.
        if blueprint is not None:
            source = _safe_path(repo_root, blueprint, label="blueprint")
            if source.is_symlink() or not source.is_dir():
                raise ExportError("blueprint must be an existing directory, not a symlink")
            copied = 0
            for item in sorted(source.rglob("*")):
                if item.is_symlink():
                    continue          # never follow links out of the build tree
                relative = item.relative_to(source)
                target = stage / "blueprint" / relative
                if item.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                elif item.is_file():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(item.read_bytes())
                    copied += 1
            if not copied:
                raise ExportError(f"blueprint directory {blueprint} contains no files")

        if output.exists():
            if not output.is_dir():
                raise ExportError("output exists and is not a directory")
            shutil.rmtree(output)
        os.replace(stage, output)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return output


def _git_commit(repo_root: Path) -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise ExportError("repository has no readable Git commit") from error


def _require_committed(repo_root: Path, graph_path: Path) -> None:
    graph_path = _safe_path(repo_root, graph_path, label="graph")
    relative = graph_path.relative_to(repo_root.resolve())
    paths = {
        relative,
        relative.parent / "informal_content",
        relative.parent / "kernel",
        relative.parent / "review_status.json",
    }
    nodes, _metadata = rm.load_graph(graph_path)
    for node_id, node in nodes.items():
        for key in ("content", "lean_file"):
            candidate = node.get(key)
            if not isinstance(candidate, str):
                continue
            base = graph_path.parent if key == "content" else repo_root
            try:
                resolved = _safe_path(base, candidate, label=f"node {node_id!r} {key}")
                paths.add(resolved.relative_to(repo_root.resolve()))
            except (ExportError, ValueError):
                continue
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "status",
            "--porcelain",
            "--",
            *(str(path) for path in sorted(paths)),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    if result.stdout.strip():
        raise ExportError("durable dashboard inputs must be committed before publication")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graph", type=Path, default=Path(".autoform/graph.json"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--blueprint", type=Path, default=None,
                        help="a BUILT leanblueprint web directory to publish at /blueprint/")
    parser.add_argument("--git-commit", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    try:
        repo_root = args.repo_root.resolve()
        graph_path = _safe_path(repo_root, args.graph, label="graph")
        output = args.output or graph_path.parent / "site"
        commit = args.git_commit or _git_commit(repo_root)
        if args.git_commit is None:
            _require_committed(repo_root, graph_path)
        result = export_site(graph_path, output, repo_root, git_commit=commit,
                             blueprint=args.blueprint)
    except (ExportError, subprocess.CalledProcessError) as error:
        parser.error(str(error))
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
