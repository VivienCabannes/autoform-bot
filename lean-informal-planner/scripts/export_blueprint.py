#!/usr/bin/env python3
"""Export a leanblueprint project from a v2 graph.json + informal_content/*.md.

Given a ``graph.json`` (schema in skills/plan/references/plan-json-schema.md) and an ``informal_content/``
directory of ``<id>.md`` files, this generates a complete, ready-to-build blueprint
project under an output directory:

    <out>/blueprint/src/web.tex
    <out>/blueprint/src/plastex.cfg
    <out>/blueprint/src/macros/common.tex
    <out>/blueprint/src/macros/web.tex
    <out>/blueprint/src/content.tex
    <out>/blueprint/web/tier_dots.js

The dependency-graph page (``dep_graph_document.html``) is produced by plasTeX
itself at build time from our custom template; we wire that template in through
the supported ``tpl=`` package option (absolute path) in ``web.tex``.

Usage:
    python export_blueprint.py <graph.json> [--content <dir>] [--out <dir>] \
        [--template <dep_graph.html>] [--title "..."]

Defaults:
    --content : <graph.json dir>/informal_content
    --out     : <graph.json dir>/blueprint_export
    --template: <repo>/templates/dep_graph.html  (sibling of scripts/)

This emits files only; the toolchain runs separately.

graph.json schema (see skills/plan/references/plan-json-schema.md), the fields we read:
    top level:
        nodes : either a dict keyed by id, OR a list of node objects.
                (Design says "a map of nodes keyed by id"; we also accept a list
                under "nodes" or "concepts" for robustness with v1-style data.)
        metadata.sources[].title / .file  -> used to build the document title.
    per node (structural):
        id              : str, unique, the concept's ordinary English name (REQUIRED)
        tier            : int (1|2|3); default 2 if absent
        parent          : str | null, container one tier up
        kind            : definition|theorem|proposition|lemma|corollary|example
        depends_on      : list[str] of node ids (edges WITHIN this tier)
        mathlib_status  : in-mathlib | partial | missing  (default: missing)
        mathlib_declarations : list[str]
        content         : path to the .md file (optional; we fall back to
                          informal_content/<id>.md and then to <slug>.md)
    A missing node's "readiness" is computed from its dependencies, not stored: it
    is ready when every prerequisite already has a formalized statement (in-mathlib
    or partial). This matches the design mapping:
        in-mathlib -> \\lean{decls} + \\mathlibok        (green)
        partial    -> \\leanok                            (green border)
        missing, all deps formalized -> nothing          (blueprint auto-blue "ready")
        missing, some dep not formalized -> \\notready    (orange "blocked")
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Status colors for the generated DOT strings. These mirror exactly what the
# leanblueprint colorizer/fillcolorizer (Packages/blueprint.py) produces, so our
# authoritative DOTs render identically to a plasTeX-generated one.
#   in-mathlib => dark-green border, light-green fill, box, filled
#   partial    => green border (statement formalized / \leanok)
#   missing, blocked (a dep not yet formalized) => orange border (\notready)
#   missing, ready (all deps formalized)        => blue border (blueprint's can_state),
#                          which we compute ourselves from dependency statuses
# ---------------------------------------------------------------------------
MATHLIB_COLOR = "darkgreen"
MATHLIB_FILL = "#B0ECA3"
PARTIAL_COLOR = "green"
NOTREADY_COLOR = "#FFAA33"
READY_COLOR = "blue"  # auto "ready to state" border, blueprint's can_state color

# graphviz shapes by kind (definitions are boxes, everything else an ellipse).
DEFINITION_KINDS = {"definition"}

VALID_STATUSES = {"in-mathlib", "partial", "missing"}

# Characters allowed in a slug-safe LaTeX label: [A-Za-z0-9_:.]
_SLUG_KEEP = re.compile(r"[^A-Za-z0-9_:.]+")


# ---------------------------------------------------------------------------
# graph.json loading / normalization
# ---------------------------------------------------------------------------

def load_graph(path: Path) -> Tuple[Dict[str, dict], dict]:
    """Load graph.json and return (nodes_by_id, metadata).

    Accepts ``nodes`` as a dict keyed by id (the design's canonical form), or as
    a list under ``nodes`` / ``concepts`` (robustness with v1-style data). The
    returned dict is keyed by the node's ``id`` and each value carries an
    ``id`` field.
    """
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        sys.exit(f"error: graph file not found: {path}")
    except json.JSONDecodeError as exc:
        sys.exit(f"error: graph file is not valid JSON: {path}: {exc}")

    metadata = raw.get("metadata", {})

    raw_nodes = raw.get("nodes", raw.get("concepts"))
    if raw_nodes is None:
        sys.exit("error: graph.json has no 'nodes' (or 'concepts') field")

    nodes: Dict[str, dict] = {}
    if isinstance(raw_nodes, dict):
        for nid, node in raw_nodes.items():
            node = dict(node)
            node.setdefault("id", nid)
            if node["id"] != nid:
                sys.exit(
                    f"error: node key {nid!r} disagrees with its 'id' field "
                    f"{node['id']!r}"
                )
            nodes[nid] = node
    elif isinstance(raw_nodes, list):
        for node in raw_nodes:
            nid = node.get("id")
            if not nid:
                sys.exit("error: a node in the list has no 'id'")
            if nid in nodes:
                sys.exit(f"error: duplicate node id {nid!r}")
            nodes[nid] = dict(node)
    else:
        sys.exit("error: 'nodes' must be a dict or a list")

    return nodes, metadata


# ---------------------------------------------------------------------------
# slugging
# ---------------------------------------------------------------------------

def make_slug(name: str) -> str:
    """Produce a slug-safe label ([A-Za-z0-9_:.]) for a node id.

    Spaces and other characters collapse to underscores; the result is never
    empty. Uniqueness across the whole graph is enforced by the caller.
    """
    slug = _SLUG_KEEP.sub("_", name).strip("_")
    if not slug:
        slug = "node"
    # A leading digit is legal in our allowed set, but prefix for safety in DOT.
    if slug[0].isdigit():
        slug = "n_" + slug
    return slug


def build_slug_map(nodes: Dict[str, dict]) -> Dict[str, str]:
    """Build a stable, collision-free id -> slug map for every node.

    Iterate in sorted-id order so the mapping is deterministic. On collision,
    append ``_2``, ``_3``, ... to keep slugs unique (\\label / DOT node ids must
    be unique across the graph).
    """
    name_to_slug: Dict[str, str] = {}
    used: set[str] = set()
    for nid in sorted(nodes):
        base = make_slug(nid)
        slug = base
        n = 2
        while slug in used:
            slug = f"{base}_{n}"
            n += 1
        used.add(slug)
        name_to_slug[nid] = slug
    return name_to_slug


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------

def node_tier(node: dict) -> int:
    return int(node.get("tier", 2))


def node_status(node: dict) -> str:
    status = node.get("mathlib_status", "missing")
    if status not in VALID_STATUSES:
        # Treat anything unexpected (e.g. v1 "unchecked") as missing.
        return "missing"
    return status


def node_ready_from_deps(node: dict, nodes: Dict[str, dict]) -> bool:
    """Whether a *missing* node is ready to be stated/formalized — every prerequisite
    already has a formalized statement (in-mathlib or partial).

    Readiness is computed from the dependency statuses; there is no stored "ready"
    field. A missing node with all deps formalized (including the no-deps case) is
    ready (blueprint auto-blues it); one with a still-missing dependency is blocked
    (=> \\notready, orange). in-mathlib / partial nodes are never \\notready.
    """
    if node_status(node) != "missing":
        return True
    for d in node.get("depends_on", []) or []:
        dep = nodes.get(d)
        if dep is not None and node_status(dep) not in ("in-mathlib", "partial"):
            return False
    return True


def validate_labels(nodes: Dict[str, dict], name_to_slug: Dict[str, str]) -> None:
    """Pre-pass: every depends_on target must exist and have a label.

    We error on a dangling edge (a target with no node / no slug). Cross-tier
    targets are allowed to exist (they have slugs too), but tier-2 content.tex
    only emits \\uses for targets that themselves appear as a tier-2 \\label;
    that filtering happens at emit time. Here we just guarantee no dangling refs.
    """
    dangling: List[str] = []
    for nid, node in nodes.items():
        for dep in node.get("depends_on", []) or []:
            if dep not in name_to_slug:
                dangling.append(f"{nid!r} -> {dep!r}")
    if dangling:
        joined = "\n  ".join(dangling)
        sys.exit(
            "error: dangling depends_on target(s) with no node/label:\n  "
            + joined
        )


# ---------------------------------------------------------------------------
# content.tex
# ---------------------------------------------------------------------------

KIND_TO_ENV = {
    "definition": "definition",
    "theorem": "theorem",
    "proposition": "proposition",
    "lemma": "lemma",
    "corollary": "corollary",
    "example": "example",
}


def topo_order(tier2: Dict[str, dict]) -> List[str]:
    """Return tier-2 node ids in dependency order (prerequisites first).

    Only edges *within* the tier-2 set are used for ordering. Ties broken by
    sorted id for determinism. Cycles (which should not occur) are broken by
    falling back to sorted order for the remaining nodes.
    """
    ids = set(tier2)
    indeg: Dict[str, int] = {i: 0 for i in ids}
    succ: Dict[str, List[str]] = {i: [] for i in ids}
    for nid, node in tier2.items():
        for dep in node.get("depends_on", []) or []:
            if dep in ids and dep != nid:
                indeg[nid] += 1
                succ[dep].append(nid)

    ready = sorted([i for i in ids if indeg[i] == 0])
    order: List[str] = []
    while ready:
        cur = ready.pop(0)
        order.append(cur)
        newly = []
        for nxt in succ[cur]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                newly.append(nxt)
        if newly:
            ready = sorted(set(ready) | set(newly))

    if len(order) != len(ids):
        # Cycle: append the rest in sorted order so we still emit everything.
        remaining = sorted(ids - set(order))
        order.extend(remaining)
    return order


def read_content(node: dict, content_dir: Path, slug: str, project_root: Path) -> str:
    """Read the markdown body for a node. Returns a fallback placeholder if the
    file is missing so the project still builds.

    Lookup order: node['content'] resolved relative to the project root (the
    schema-canonical form, e.g. "informal_content/foo.md"), then relative to
    content_dir, then content_dir/<id>.md, then content_dir/<slug>.md.
    """
    candidates: List[Path] = []
    cpath = node.get("content")
    if cpath:
        p = Path(cpath)
        if p.is_absolute():
            candidates.append(p)
        else:
            candidates.append(project_root / p)
            candidates.append(content_dir / p)
    candidates.append(content_dir / f"{node['id']}.md")
    candidates.append(content_dir / f"{slug}.md")
    for c in candidates:
        if c.is_file():
            return c.read_text().strip()
    return f"% TODO: missing content for {node['id']}\n\\emph{{(statement pending)}}"


def emit_content_tex(
    tier2_order: List[str],
    nodes: Dict[str, dict],
    name_to_slug: Dict[str, str],
    content_dir: Path,
    project_root: Path,
) -> str:
    """Build content.tex: one blueprint environment per tier-2 node, in
    dependency order, with vanilla blueprint annotations.
    """
    tier2_ids = set(tier2_order)
    blocks: List[str] = [
        "% Generated by export_blueprint.py -- do not edit by hand.",
        "% One blueprint environment per tier-2 node, in dependency order.",
        "",
    ]

    for nid in tier2_order:
        node = nodes[nid]
        slug = name_to_slug[nid]
        env = KIND_TO_ENV.get((node.get("kind") or "theorem").lower(), "theorem")
        status = node_status(node)

        # \uses targets: only tier-2 depends_on (within-tier edges) that are
        # themselves emitted as labels in this document.
        uses = [
            name_to_slug[d]
            for d in (node.get("depends_on", []) or [])
            if d in tier2_ids and d != nid
        ]

        body = read_content(node, content_dir, slug, project_root)

        lines: List[str] = []
        lines.append(f"\\begin{{{env}}}\\label{{{slug}}}")
        # Comment naming the human id for traceability (LaTeX-safe).
        lines.append(f"  % node id: {node['id']}")
        if body:
            lines.append("  " + body.replace("\n", "\n  "))
        if uses:
            lines.append(f"  \\uses{{{','.join(uses)}}}")

        # Status annotations per the design mapping.
        if status == "in-mathlib":
            decls = node.get("mathlib_declarations", []) or []
            if decls:
                lines.append(f"  \\lean{{{','.join(d.strip() for d in decls)}}}")
            lines.append("  \\mathlibok")
        elif status == "partial":
            lines.append("  \\leanok")
        else:  # missing
            if not node_ready_from_deps(node, nodes):
                lines.append("  \\notready")
            # missing + ready: emit nothing (blueprint auto-derives the "ready" blue).

        lines.append(f"\\end{{{env}}}")
        blocks.append("\n".join(lines))
        blocks.append("")

    return "\n".join(blocks) + "\n"


# ---------------------------------------------------------------------------
# DOT generation (per-tier sidecar tier_dots.js)
# ---------------------------------------------------------------------------

def _dot_escape(s: str) -> str:
    r"""Escape a string for a double-quoted graphviz id/label."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _node_dot(
    nid: str,
    node: dict,
    slug: str,
    ready: bool,
) -> str:
    """Emit one DOT node line. Status colors match the prototype/blueprint:

        in-mathlib => color=darkgreen, fillcolor="#B0ECA3", shape=box, style=filled
        partial    => color=green
        missing + not ready => color="#FFAA33"
        missing + ready     => color=blue (auto "ready")
    The graphviz node id is the slug; the visible label is the human id.
    """
    status = node_status(node)
    kind = (node.get("kind") or "theorem").lower()
    shape = "box" if kind in DEFINITION_KINDS else "ellipse"

    attrs: List[str] = [f'label="{_dot_escape(nid)}"']

    if status == "in-mathlib":
        attrs.append(f"color={MATHLIB_COLOR}")
        attrs.append(f'fillcolor="{MATHLIB_FILL}"')
        attrs.append("shape=box")
        attrs.append("style=filled")
    elif status == "partial":
        attrs.append(f"color={PARTIAL_COLOR}")
        attrs.append(f"shape={shape}")
    else:  # missing
        if ready:
            attrs.append(f"color={READY_COLOR}")
        else:
            attrs.append(f'color="{NOTREADY_COLOR}"')
        attrs.append(f"shape={shape}")

    return f'  "{slug}" [{", ".join(attrs)}];'


def _ready_set(tier2: Dict[str, dict]) -> set[str]:
    """Compute the set of *missing* tier-2 ids that are auto-"ready" (blue).

    Mirrors blueprint's ``can_state``: a missing node is ready iff every within-tier
    prerequisite is "leanok" (in-mathlib or partial). Readiness is computed from the
    dependency statuses; there is no stored "ready" field. Only missing nodes are
    colored blue, so this matters only for them.
    """
    return {
        nid for nid, node in tier2.items()
        if node_status(node) == "missing" and node_ready_from_deps(node, tier2)
    }


def build_tier2_dot(
    tier2: Dict[str, dict],
    name_to_slug: Dict[str, str],
) -> str:
    """Tier-2 DOT: all tier-2 nodes + within-tier edges, with status colors."""
    ready = _ready_set(tier2)
    lines: List[str] = ['strict digraph "" {']
    lines.append("  graph [bgcolor=transparent];")
    lines.append('  node [label="\\N", penwidth=1.8];')
    lines.append("  edge [arrowhead=vee];")

    ids = set(tier2)
    for nid in sorted(tier2):
        lines.append(_node_dot(nid, tier2[nid], name_to_slug[nid], nid in ready))

    edges: List[Tuple[str, str]] = []
    for nid, node in tier2.items():
        for dep in node.get("depends_on", []) or []:
            if dep in ids and dep != nid:
                edges.append((name_to_slug[dep], name_to_slug[nid]))
    for s, t in sorted(set(edges)):
        lines.append(f'  "{s}" -> "{t}" [style=dashed];')

    lines.append("}")
    return "\n".join(lines)


def build_tier1_dot(
    tier1: Dict[str, dict],
    tier2: Dict[str, dict],
    name_to_slug: Dict[str, str],
) -> str:
    """Tier-1 DOT: the quotient of tier-2.

    Nodes are the tier-1 clusters. An edge tier1 A -> B exists iff some tier-2
    node in A depends on some tier-2 node in B (A != B). Tier-1 node colors are
    aggregated from their members (all in-mathlib => mathlib; else if any partial
    or some-but-not-all in-mathlib => partial/green; else not-ready orange) so the
    cluster view conveys progress. Definitions vs theorems is meaningless at the
    cluster level, so clusters are boxes.
    """
    # Map each tier-2 id to its parent tier-1 id (if the parent is a tier-1 node).
    parent_of: Dict[str, str] = {}
    for nid, node in tier2.items():
        p = node.get("parent")
        if p in tier1:
            parent_of[nid] = p

    lines: List[str] = ['strict digraph "" {']
    lines.append("  graph [bgcolor=transparent];")
    lines.append('  node [label="\\N", penwidth=1.8];')
    lines.append("  edge [arrowhead=vee];")

    for cid in sorted(tier1):
        members = [m for m, p in parent_of.items() if p == cid]
        slug = name_to_slug[cid]
        attrs = [f'label="{_dot_escape(cid)}"', "shape=box"]
        if members:
            statuses = {node_status(tier2[m]) for m in members}
            if statuses == {"in-mathlib"}:
                attrs.append(f"color={MATHLIB_COLOR}")
                attrs.append(f'fillcolor="{MATHLIB_FILL}"')
                attrs.append("style=filled")
            elif statuses <= {"in-mathlib", "partial"}:
                attrs.append(f"color={PARTIAL_COLOR}")
            else:
                attrs.append(f'color="{NOTREADY_COLOR}"')
        else:
            # Cluster with no tier-2 members yet (Phase 1): authored color from
            # the cluster's own status if present, else not-ready orange.
            st = node_status(tier1[cid])
            if st == "in-mathlib":
                attrs.append(f"color={MATHLIB_COLOR}")
                attrs.append(f'fillcolor="{MATHLIB_FILL}"')
                attrs.append("style=filled")
            elif st == "partial":
                attrs.append(f"color={PARTIAL_COLOR}")
            else:
                attrs.append(f'color="{NOTREADY_COLOR}"')
        lines.append(f'  "{slug}" [{", ".join(attrs)}];')

    # Quotient edges: collapse tier-2 edges onto their parents.
    cluster_edges: set[Tuple[str, str]] = set()
    ids2 = set(tier2)
    for nid, node in tier2.items():
        src_c = parent_of.get(nid)
        if src_c is None:
            continue
        for dep in node.get("depends_on", []) or []:
            if dep not in ids2:
                continue
            tgt_c = parent_of.get(dep)
            if tgt_c is None or tgt_c == src_c:
                continue
            cluster_edges.add((name_to_slug[tgt_c], name_to_slug[src_c]))

    # If there are no tier-2 nodes at all (Phase 1), fall back to the authored
    # tier-1 edges so the coarse graph still has its structure.
    if not tier2:
        for cid, node in tier1.items():
            for dep in node.get("depends_on", []) or []:
                if dep in tier1 and dep != cid:
                    cluster_edges.add((name_to_slug[dep], name_to_slug[cid]))

    for s, t in sorted(cluster_edges):
        lines.append(f'  "{s}" -> "{t}" [style=dashed];')

    lines.append("}")
    return "\n".join(lines)


def emit_tier_dots_js(
    nodes: Dict[str, dict],
    name_to_slug: Dict[str, str],
) -> Tuple[str, List[int]]:
    """Build tier_dots.js. Returns (js_text, sorted_tiers_present).

    Exposes:
        const DOTS = {1: "...", 2: "..."};         // by tier number
        const DOT_TIER1 = DOTS[1]; ...             // named aliases
        const TIER_CLUSTER_MEMBERS = {1: {slug: [member-human-ids...]}};
        const TIER_NODE_NAMES = {slug: "human id", ...};
        const TIERS_PRESENT = [1, 2, ...];
    The cluster-member map lets the custom template show a summary when a coarse
    (tier-1) cluster node -- which has no per-statement modal -- is clicked.
    """
    by_tier: Dict[int, Dict[str, dict]] = {}
    for nid, node in nodes.items():
        by_tier.setdefault(node_tier(node), {})[nid] = node

    tiers = sorted(by_tier)
    tier1 = by_tier.get(1, {})
    tier2 = by_tier.get(2, {})

    dots: Dict[int, str] = {}
    if tier2:
        dots[2] = build_tier2_dot(tier2, name_to_slug)
    if tier1:
        dots[1] = build_tier1_dot(tier1, tier2, name_to_slug)
    # Any further tiers (e.g. 3) are emitted as flat per-tier graphs using the
    # same status-colored node renderer, so the toggle never errors on them.
    for t in tiers:
        if t in (1, 2):
            continue
        dots[t] = build_tier2_dot(by_tier[t], name_to_slug)

    # cluster -> member human ids, for the cluster-summary click handler.
    cluster_members: Dict[str, List[str]] = {}
    for nid, node in tier2.items():
        p = node.get("parent")
        if p in tier1:
            cluster_members.setdefault(name_to_slug[p], []).append(nid)
    for slug in cluster_members:
        cluster_members[slug].sort()

    node_names = {name_to_slug[nid]: nid for nid in nodes}

    # Per-node descriptions (the one-line informal summary), shown when a cluster
    # node is clicked. Provisional contents let a Phase-1 cluster show the statements
    # it is expected to contain before any tier-2 node exists.
    node_desc = {
        name_to_slug[nid]: (node.get("description") or "")
        for nid, node in nodes.items()
        if node.get("description")
    }
    provisional = {
        name_to_slug[nid]: list(node.get("provisional_members") or [])
        for nid, node in tier1.items()
        if node.get("provisional_members")
    }

    present = sorted(dots)
    parts: List[str] = [
        "// Generated by export_blueprint.py -- do not edit by hand.",
        "// Per-tier graphviz DOT strings + cluster metadata for the tier toggle.",
        "",
    ]
    for t in present:
        parts.append(f"const DOT_TIER{t} = {json.dumps(dots[t])};")
    parts.append("")
    dots_obj = "{ " + ", ".join(f"{t}: DOT_TIER{t}" for t in present) + " }"
    parts.append(f"const DOTS = {dots_obj};")
    parts.append(f"const TIERS_PRESENT = {json.dumps(present)};")
    parts.append(f"const TIER_CLUSTER_MEMBERS = {{ 1: {json.dumps(cluster_members)} }};")
    parts.append(f"const TIER_NODE_NAMES = {json.dumps(node_names)};")
    parts.append(f"const TIER_NODE_DESC = {json.dumps(node_desc)};")
    parts.append(f"const TIER_PROVISIONAL = {json.dumps(provisional)};")
    parts.append("")
    return "\n".join(parts), present


# ---------------------------------------------------------------------------
# static project files (web.tex, plastex.cfg, macros)
# ---------------------------------------------------------------------------

COMMON_TEX = r"""% Generated by export_blueprint.py -- do not edit by hand.
% Shared math macros + theorem environments tracked by the dependency graph.
\newtheorem{theorem}{Theorem}
\newtheorem{proposition}[theorem]{Proposition}
\newtheorem{lemma}[theorem]{Lemma}
\newtheorem{corollary}[theorem]{Corollary}

\theoremstyle{definition}
\newtheorem{definition}[theorem]{Definition}
\newtheorem{example}[theorem]{Example}
"""

WEB_MACROS_TEX = r"""% Generated by export_blueprint.py -- do not edit by hand.
% Web-only macros (none needed yet).
"""

PLASTEX_CFG = """[general]
renderer=HTML5
copy-theme-extras=yes
plugins=plastexdepgraph leanblueprint

[document]
toc-depth=2
toc-non-files=True

[files]
directory=../web/
split-level=0

[html5]
localtoc-level=1
mathjax-dollars=False
extra-js=tier_dots.js
"""


def build_web_tex(template_abs: Path, title: str) -> Tuple[str, str]:
    r"""Return (web.tex content, the exact blueprint package options line).

    The custom template is wired in via the supported ``tpl=`` package option,
    passed through the blueprint package to depgraph, using an ABSOLUTE path so it
    resolves under the leanblueprint CLI's chdir.
    """
    options_line = f"\\usepackage[dep_graph,tpl={template_abs}]{{blueprint}}"
    content = (
        "% Generated by export_blueprint.py -- do not edit by hand.\n"
        "\\documentclass{article}\n"
        "\n"
        "\\usepackage{amssymb, amsthm, amsmath}\n"
        "\\usepackage{hyperref}\n"
        f"{options_line}\n"
        "\n"
        "\\input{macros/common}\n"
        "\\input{macros/web}\n"
        "\n"
        "\\home{http://example.com}\n"
        "\\github{http://example.com}\n"
        "\\dochome{https://leanprover-community.github.io/mathlib4_docs}\n"
        "\n"
        f"\\title{{{_latex_escape_title(title)}}}\n"
        "\\author{Lean Informal Planner}\n"
        "\n"
        "\\begin{document}\n"
        "\\maketitle\n"
        "\\input{content}\n"
        "\\end{document}\n"
    )
    return content, options_line


def _latex_escape_title(title: str) -> str:
    """Escape the few characters that would break a LaTeX title."""
    repl = {
        "\\": r"\textbackslash{}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }
    out = []
    for ch in title:
        out.append(repl.get(ch, ch))
    return "".join(out)


def build_title(metadata: dict) -> str:
    sources = metadata.get("sources", []) or []
    titles = [s.get("title") or s.get("file") for s in sources if isinstance(s, dict)]
    titles = [t for t in titles if t]
    if titles:
        return ", ".join(titles)
    return metadata.get("title", "Formalization Blueprint")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Export a leanblueprint project from graph.json")
    parser.add_argument("graph", type=Path, help="path to graph.json")
    parser.add_argument("--content", type=Path, default=None,
                        help="prose dir of <id>.md files (default: <graph dir>/informal_content)")
    parser.add_argument("--out", type=Path, default=None,
                        help="output dir (default: <graph dir>/blueprint_export)")
    parser.add_argument("--template", type=Path, default=None,
                        help="custom dep_graph.html (default: <repo>/templates/dep_graph.html)")
    parser.add_argument("--title", default=None, help="override document title")
    args = parser.parse_args(argv)

    graph_path = args.graph.resolve()
    content_dir = (args.content or graph_path.parent / "informal_content").resolve()
    out_dir = (args.out or graph_path.parent / "blueprint_export").resolve()

    # Default template lives at <repo>/templates/dep_graph.html (sibling of scripts/).
    repo_root = Path(__file__).resolve().parent.parent
    template_path = (args.template or repo_root / "templates" / "dep_graph.html").resolve()

    nodes, metadata = load_graph(graph_path)
    if not nodes:
        sys.exit("error: graph.json has no nodes")

    name_to_slug = build_slug_map(nodes)
    validate_labels(nodes, name_to_slug)

    by_tier: Dict[int, Dict[str, dict]] = {}
    for nid, node in nodes.items():
        by_tier.setdefault(node_tier(node), {})[nid] = node
    tier2 = by_tier.get(2, {})

    title = args.title or build_title(metadata)

    # Lay out directories.
    src_dir = out_dir / "blueprint" / "src"
    macros_dir = src_dir / "macros"
    web_dir = out_dir / "blueprint" / "web"
    for d in (macros_dir, web_dir):
        d.mkdir(parents=True, exist_ok=True)

    # content.tex (tier-2 in dependency order). If there are no tier-2 nodes yet
    # (Phase 1), content.tex is empty-but-valid; the tier-1 DOT still renders.
    tier2_order = topo_order(tier2) if tier2 else []
    content_tex = emit_content_tex(tier2_order, nodes, name_to_slug, content_dir,
                                   graph_path.parent)
    (src_dir / "content.tex").write_text(content_tex)

    # tier_dots.js sidecar. Written into src/ so plasTeX's `extra-js` copies it
    # to web/js/, where the template loads it as js/tier_dots.js.
    js_text, tiers_present = emit_tier_dots_js(nodes, name_to_slug)
    src_dir.mkdir(parents=True, exist_ok=True)
    (src_dir / "tier_dots.js").write_text(js_text)

    # web.tex + plastex.cfg + macros.
    web_tex, options_line = build_web_tex(template_path, title)
    (src_dir / "web.tex").write_text(web_tex)
    (src_dir / "plastex.cfg").write_text(PLASTEX_CFG)
    (macros_dir / "common.tex").write_text(COMMON_TEX)
    (macros_dir / "web.tex").write_text(WEB_MACROS_TEX)

    print(f"Exported blueprint project to: {out_dir}")
    print(f"  tier-2 nodes: {len(tier2)}  tiers present: {tiers_present}")
    print(f"  blueprint package options: {options_line}")
    print(f"  template: {template_path}")
    if not template_path.is_file():
        print(f"  WARNING: template not found at {template_path}; "
              "build will fall back to the stock plastexdepgraph template.",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
