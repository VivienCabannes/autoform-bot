#!/usr/bin/env python3
"""Local review server for the DAG-native review surface.

Serves the three review screens over a stdlib ThreadingHTTPServer bound to
``127.0.0.1`` (local only — never exposed). The graph and the *built* blueprint
fragments are read-only; the **only** file this server ever writes is the sidecar
``review_status.json`` (the single source of truth for verdicts).

Three screens (SHARED_SPEC):
  * ``GET /``               — home: the dep-graph recolored by effective verdict
                              (AI-only dashed, human solid, tainted hatched, mathlib
                              lanes), with the coverage bar + trust-frontier header.
  * ``GET /cluster/<id>``   — a tier-1 cluster's children + roll-up ("review deck").
  * ``GET /node/<id>``      — the packet: rendered blueprint theorem env (left) +
                              source_refs / mathlib decl / kernel evidence (right) +
                              the verdict panel.

API:
  * ``GET  /api/state``         — computed verdicts + taint + coverage + frontier.
  * ``POST /api/verdict/<id>``  — write the human slot, recompute taint, return the
                                  delta (new effective verdicts + tainted set).
  * ``GET  /assets/*``          — review.css / review.js (+ any static asset).

Inputs (read-only): ``graph.json``, ``informal_content/<id>.md``, the built
blueprint (``dep_graph_document.html`` for the ``div.thm#<slug>`` fragments), an
optional ``kernel/<id>.txt`` (``#print axioms`` dump), and the sidecar.

Run:
    python serve_review.py --graph path/to/graph.json
    python serve_review.py --graph graph.json --port 8765 --open
"""
from __future__ import annotations

import argparse
import html as htmllib
import json
import re
import sys
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, Optional

_HERE = Path(__file__).resolve().parent
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))  # scripts/ on path for export_blueprint

import review_model as rm  # noqa: E402

# Repo root = .../scripts/review_ui -> up two. Assets live at <root>/assets/review/.
_REPO_ROOT = _HERE.parent.parent
_ASSETS_DIR = _REPO_ROOT / "assets" / "review"

# Regex to pull a single built blueprint fragment <div class="thm" id="slug" ...>
# ... </div> out of dep_graph_document.html. The exported template renders each
# tier-2 node as `<div class="thm" id="{{ slug }}" ...>` (MathJax already run), so
# we inject that fragment verbatim — never regenerating the informalization.
_THM_OPEN = '<div class="thm"'


# ---------------------------------------------------------------------------
# project context — resolves all the read-only inputs around graph.json
# ---------------------------------------------------------------------------

class Project:
    """Resolves the read-only inputs + owns the single sidecar write.

    Everything is recomputed from disk on each request (cheap, and keeps the server
    correct if the graph or a re-run of the jury changes the files under it).
    """

    def __init__(self, graph_path: Path):
        self.graph_path = graph_path.resolve()
        self.root = self.graph_path.parent
        self.content_dir = self.root / "informal_content"
        self.kernel_dir = self.root / "kernel"
        self.sidecar_path = self.root / "review_status.json"
        # Built blueprint dep-graph page (source of div.thm#<slug> fragments).
        self.blueprint_html = (
            self.root / "blueprint_export" / "blueprint" / "web"
            / "dep_graph_document.html"
        )
        self._slug_cache: Optional[Dict[str, str]] = None

    # --- graph + sidecar ---
    def nodes(self):
        nodes, _meta = rm.load_graph(self.graph_path)
        return nodes

    def metadata(self):
        _nodes, meta = rm.load_graph(self.graph_path)
        return meta

    def sidecar(self) -> dict:
        return rm.load_sidecar(self.sidecar_path)

    def write_sidecar(self, data: dict) -> None:
        """The one and only write this server performs — atomic, via review_model."""
        rm.save_sidecar(self.sidecar_path, data)

    def slug_map(self) -> Dict[str, str]:
        # Slugs are a deterministic function of the id set; recompute per request so
        # an added node is reflected without a restart.
        import export_blueprint as eb
        return eb.build_slug_map(self.nodes())

    # --- the three read-only artifacts of a node ---
    def blueprint_fragment(self, slug: str) -> Optional[str]:
        """Extract the built ``<div class="thm" id="<slug>">...</div>`` fragment.

        Returns None if the blueprint has not been built or the node has no env.
        MathJax has already run in the built page, so the fragment is injected
        verbatim — we never regenerate the informalization.
        """
        if not self.blueprint_html.is_file():
            return None
        text = self.blueprint_html.read_text(errors="replace")
        needle = f'id="{slug}"'
        # Find a `<div class="thm" ... id="slug"` open tag, then balance divs.
        idx = 0
        while True:
            open_at = text.find(_THM_OPEN, idx)
            if open_at == -1:
                return None
            tag_end = text.find(">", open_at)
            if tag_end == -1:
                return None
            open_tag = text[open_at:tag_end]
            if needle in open_tag:
                return _extract_balanced_div(text, open_at)
            idx = tag_end + 1

    def informal_md(self, node_id: str, node: dict) -> Optional[str]:
        """Raw informal_content markdown for a node (fallback when no blueprint)."""
        cand = []
        cpath = node.get("content")
        if cpath:
            cand.append(self.root / cpath)
        cand.append(self.content_dir / f"{node_id}.md")
        for c in cand:
            if c.is_file():
                return c.read_text(errors="replace")
        return None

    def kernel_evidence(self, node_id: str) -> Optional[str]:
        """The ``#print axioms`` dump for a node, if a ``kernel/<id>.txt`` exists."""
        p = self.kernel_dir / f"{node_id}.txt"
        if p.is_file():
            return p.read_text(errors="replace")
        return None


def _extract_balanced_div(text: str, open_at: int) -> str:
    """Return the substring covering one balanced <div>...</div> from ``open_at``."""
    depth = 0
    i = open_at
    n = len(text)
    div_open = re.compile(r"<div\b", re.IGNORECASE)
    div_close = re.compile(r"</div\s*>", re.IGNORECASE)
    while i < n:
        mo = div_open.match(text, i)
        mc = div_close.match(text, i)
        if mo:
            depth += 1
            i = text.find(">", i)
            if i == -1:
                break
            i += 1
        elif mc:
            depth -= 1
            i = mc.end()
            if depth == 0:
                return text[open_at:i]
        else:
            i += 1
    return text[open_at:]


# ---------------------------------------------------------------------------
# HTML rendering of the three screens (server-side; assets add interactivity)
# ---------------------------------------------------------------------------

_E = htmllib.escape


def _page(title: str, body: str, bootstrap: str = "") -> bytes:
    """Wrap a screen body in the shared shell (links review.css/js)."""
    boot = f"<script>{bootstrap}</script>" if bootstrap else ""
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{_E(title)}</title>"
        "<link rel='stylesheet' href='/assets/review.css'>"
        "<script>window.MathJax={tex:{inlineMath:[['$','$'],['\\\\(','\\\\)']],"
        "displayMath:[['$$','$$'],['\\\\[','\\\\]']]},"
        "options:{skipHtmlTags:['script','noscript','style','textarea','pre','code']}};</script>"
        "<script async src='https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js'></script>"
        "</head><body>"
        "<header class='rv-header'><a class='rv-home' href='/'>review</a>"
        f"<span class='rv-title'>{_E(title)}</span></header>"
        f"<main class='rv-main'>{body}</main>"
        f"{boot}"
        "<script src='/assets/review.js'></script>"
        "</body></html>"
    ).encode("utf-8")


def _render_md(md: str) -> str:
    """Minimal, safe Markdown -> HTML for the informal_content fallback: headings and
    paragraphs only, HTML-escaped, with ``$...$`` / ``$$...$$`` kept intact for MathJax
    (so it must NOT be wrapped in <pre>, which MathJax is configured to skip)."""
    out = []
    para = []

    def _flush():
        if para:
            out.append("<p>" + "<br>".join(_E(x) for x in para) + "</p>")
            para.clear()

    for line in md.strip().split("\n"):
        s = line.strip()
        m = re.match(r"^(#{1,6})\s+(.*)$", s)
        if not s:
            _flush()
        elif m:
            _flush()
            lvl = min(len(m.group(1)) + 2, 6)
            out.append(f"<h{lvl}>{_E(m.group(2).strip())}</h{lvl}>")
        else:
            para.append(s)
    _flush()
    return "\n".join(out) or "<p><em>(empty)</em></p>"


def render_home(proj: Project, tier: int = 2) -> bytes:
    nodes = proj.nodes()
    sidecar = proj.sidecar()
    state = rm.compute_state(nodes, sidecar)
    dot = rm.recolor_dot(nodes, sidecar, tier=tier)
    slug_map = proj.slug_map()
    # id<->slug both directions so the client can route node clicks to /node/<id>.
    id_by_slug = {slug: nid for nid, slug in slug_map.items()}

    boot = (
        f"window.__RV_DOT__ = {json.dumps(dot)};"
        f"window.__RV_STATE__ = {json.dumps(state)};"
        f"window.__RV_IDBYSLUG__ = {json.dumps(id_by_slug)};"
        f"window.__RV_PALETTE__ = {json.dumps(rm.PALETTE)};"
        f"window.__RV_TIER__ = {tier};"
    )
    cov = state["coverage"]
    frontier = state["trust_frontier"]
    body = (
        "<section class='rv-headerbar'>"
        "<div class='rv-coverage'>"
        f"<span class='rv-cov-label'>coverage</span>"
        f"<div class='rv-cov-bar'><div class='rv-cov-fill' "
        f"style='width:{cov['fraction']*100:.0f}%'></div></div>"
        f"<span class='rv-cov-num'>{cov['reviewed']}/{cov['total']} reviewed"
        f" · {cov['human_confirmed']} human-confirmed</span>"
        "</div>"
        f"<div class='rv-frontier'><span class='rv-fr-label'>trust frontier</span> "
        + (", ".join(f"<a href='/node/{urllib.parse.quote(f)}'>{_E(f)}</a>"
                     for f in frontier) if frontier
           else "<em>none yet — no sink rests on a fully-clean closure</em>")
        + "</div>"
        f"<div class='rv-dial'>dial: <strong>{_E(state['dial'])}</strong></div>"
        "</section>"
        + ("<div class='rv-tiertoggle'>view: "
           + ("<strong>Tier 1 · clusters</strong>" if tier == 1
              else "<a href='/?tier=1'>Tier 1 · clusters</a>")
           + " &nbsp;⇄&nbsp; "
           + ("<strong>Tier 2 · statements</strong>" if tier == 2
              else "<a href='/'>Tier 2 · statements</a>")
           + "</div>")
        + _legend_html()
        + "<div id='rv-graph' class='rv-graph'></div>"
    )
    return _page("dependency graph", body, boot)


def _legend_html() -> str:
    return (
        "<div class='rv-legend'>"
        "<span class='rv-key rv-in_mathlib'>in Mathlib</span>"
        "<span class='rv-key rv-clean'>ours · clean</span>"
        "<span class='rv-key rv-flagged'>flagged</span>"
        "<span class='rv-key rv-rejected'>rejected</span>"
        "<span class='rv-key rv-grey'>unreviewed</span>"
        "<span class='rv-key rv-dash'>AI-only (dashed)</span>"
        "<span class='rv-key rv-solid'>human-confirmed</span>"
        "<span class='rv-key rv-hatch'>tainted</span>"
        "<span class='rv-lanes'>lanes: in-mathlib (bottom) → missing (top)</span>"
        "</div>"
    )


def render_cluster(proj: Project, cluster_id: str) -> bytes:
    nodes = proj.nodes()
    if cluster_id not in nodes:
        return _page("unknown cluster",
                     f"<p class='rv-err'>No cluster {_E(cluster_id)}.</p>")
    sidecar = proj.sidecar()
    roll = rm.cluster_rollup(cluster_id, nodes, sidecar)
    rows = []
    for cid in roll["children"]:
        v = roll["child_verdicts"][cid]
        src = rm.review_source(cid, sidecar) or "—"
        rows.append(
            f"<tr class='rv-row rv-{v}'>"
            f"<td><a href='/node/{urllib.parse.quote(cid)}'>{_E(cid)}</a></td>"
            f"<td class='rv-verdict-cell'>{_E(v)}</td>"
            f"<td>{_E(src)}</td></tr>"
        )
    counts = roll["counts"]
    body = (
        f"<h2 class='rv-cluster-rollup rv-{roll['verdict']}'>"
        f"cluster roll-up: {_E(roll['verdict'])}</h2>"
        f"<p class='rv-rollup-rule'>A cluster is clean only if every child is clean;"
        f" any flagged/rejected child ⇒ flagged.</p>"
        f"<p class='rv-counts'>clean {counts['clean']} · flagged {counts['flagged']}"
        f" · rejected {counts['rejected']} · unreviewed {counts['unreviewed']}</p>"
        "<table class='rv-table'><thead><tr><th>statement</th><th>verdict</th>"
        "<th>source</th></tr></thead><tbody>"
        + ("".join(rows) if rows
           else "<tr><td colspan='3'><em>no tier-2 children yet</em></td></tr>")
        + "</tbody></table>"
    )
    return _page(f"cluster · {cluster_id}", body)


def render_node(proj: Project, node_id: str) -> bytes:
    nodes = proj.nodes()
    if node_id not in nodes:
        return _page("unknown node",
                     f"<p class='rv-err'>No node {_E(node_id)}.</p>")
    node = nodes[node_id]
    sidecar = proj.sidecar()
    slug = proj.slug_map().get(node_id, node_id)
    scorecard = rm.node_scorecard(node_id, sidecar)

    # left column: built blueprint env (preferred) else raw informal markdown.
    fragment = proj.blueprint_fragment(slug)
    if fragment:
        left = f"<div class='rv-thm-env'>{fragment}</div>"
    else:
        md = proj.informal_md(node_id, node)
        if md:
            left = ("<div class='rv-thm-env rv-thm-raw'><p class='rv-note'>"
                    "blueprint not built — rendering informal_content.</p>"
                    f"{_render_md(md)}</div>")
        else:
            left = ("<div class='rv-thm-env'><em>No blueprint fragment or "
                    "informal content for this node.</em></div>")

    # right column: source_refs (verbatim) + mathlib decls + kernel evidence.
    right = _node_meta_html(proj, node_id, node, scorecard)

    boot = (
        f"window.__RV_NODE__ = {json.dumps(node_id)};"
        f"window.__RV_SCORECARD__ = {json.dumps(scorecard)};"
    )
    body = (
        "<div class='rv-packet'>"
        f"<section class='rv-packet-left'>{left}</section>"
        f"<section class='rv-packet-right'>{right}</section>"
        "</div>"
        + _verdict_panel_html(node_id, scorecard)
    )
    return _page(f"node · {node_id}", body, boot)


def _node_meta_html(proj: Project, node_id: str, node: dict, scorecard: dict) -> str:
    kind = node.get("kind", "—")
    status = node.get("mathlib_status", "missing")
    parts = [f"<h3 class='rv-node-id'>{_E(node_id)}</h3>",
             f"<p class='rv-node-sub'>{_E(kind)} · mathlib_status: "
             f"{_E(status)}</p>"]

    refs = node.get("source_refs") or []
    if refs:
        parts.append("<div class='rv-card'><h4>source_refs (verbatim)</h4><ul>")
        for r in refs:
            # source_refs entries are verbatim citations: usually a plain string,
            # but tolerate a structured {file, location} dict too.
            if isinstance(r, dict):
                f = _E(str(r.get("file", "")))
                loc = _E(str(r.get("location", "")))
                parts.append(
                    f"<li><code>{f}</code> — {loc}</li>" if loc
                    else f"<li><code>{f}</code></li>")
            else:
                parts.append(f"<li>{_E(str(r))}</li>")
        parts.append("</ul></div>")

    decls = node.get("mathlib_declarations") or []
    if decls:
        parts.append("<div class='rv-card'><h4>mathlib_declarations</h4><ul>")
        for d in decls:
            parts.append(f"<li><code>{_E(d)}</code></li>")
        parts.append("</ul></div>")
    mfile = node.get("mathlib_file")
    if mfile:
        parts.append(f"<p class='rv-mfile'>file: <code>{_E(mfile)}</code></p>")

    kernel = proj.kernel_evidence(node_id)
    parts.append("<div class='rv-card rv-kernel'><h4>kernel evidence "
                 "(<code>#print axioms</code>)</h4>")
    if kernel:
        parts.append(f"<pre>{_E(kernel)}</pre>")
    else:
        parts.append("<p class='rv-note'>No <code>kernel/" + _E(node_id)
                     + ".txt</code> dump — run the reviewer/packet path to "
                     "generate kernel evidence.</p>")
    parts.append("</div>")

    # jury scorecard card
    ai = scorecard["ai"]
    parts.append("<div class='rv-card rv-scorecard'><h4>jury scorecard</h4>"
                 "<table class='rv-score-table'>"
                 "<tr><th>rubric</th><th>score</th><th>pass</th></tr>")
    for name, weight, thr in rm.RUBRICS:
        val = ai.get(name)
        cell = "—" if val is None else str(val)
        ok = "—" if val is None else ("✓" if val >= thr else "✗")
        parts.append(f"<tr><td>{_E(name)} <span class='rv-w'>×{weight}</span></td>"
                     f"<td>{cell}</td><td>{ok}</td></tr>")
    wt = ai.get("weighted")
    parts.append(f"<tr class='rv-score-total'><td>weighted</td>"
                 f"<td>{'—' if wt is None else wt}</td>"
                 f"<td>ai: {_E(str(ai.get('verdict') or '—'))}</td></tr>")
    parts.append("</table></div>")
    return "".join(parts)


def _verdict_panel_html(node_id: str, scorecard: dict) -> str:
    human = scorecard.get("human") or {}
    cur = human.get("verdict") or ""
    note = human.get("note") or ""
    score = human.get("score")
    eff = scorecard["effective"]
    src = scorecard["source"] or "—"
    return (
        "<form class='rv-verdict-panel' id='rv-verdict-form' "
        f"data-node='{_E(node_id)}'>"
        f"<h4>verdict panel <span class='rv-eff rv-{eff}'>effective: {_E(eff)}"
        f" ({_E(src)})</span></h4>"
        "<div class='rv-vp-row'>"
        + "".join(
            f"<label class='rv-radio'><input type='radio' name='verdict' "
            f"value='{v}'{' checked' if cur == v else ''}>{v}</label>"
            for v in rm.VERDICTS)
        + "</div>"
        "<div class='rv-vp-row'><label>score (0-5) "
        f"<input type='number' name='score' min='0' max='5' "
        f"value='{'' if score is None else _E(str(score))}'></label></div>"
        f"<div class='rv-vp-row'><label>note <textarea name='note'>{_E(note)}"
        "</textarea></label></div>"
        "<div class='rv-vp-row'><button type='submit'>save human verdict</button>"
        "<span class='rv-vp-status' id='rv-vp-status'></span></div>"
        "<p class='rv-note'>Human verdict is immutable — re-running the AI never "
        "overrides it.</p>"
        "</form>"
    )


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

def make_handler(proj: Project):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _send(self, code, body, ctype="text/html; charset=utf-8"):
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code, obj):
            self._send(code, json.dumps(obj), "application/json")

        # --- GET ---
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            if path == "/":
                qs = urllib.parse.parse_qs(parsed.query)
                tier = 1 if qs.get("tier", ["2"])[0] == "1" else 2
                return self._send(200, render_home(proj, tier))
            if path == "/api/state":
                return self._json(200, rm.compute_state(proj.nodes(), proj.sidecar()))
            if path.startswith("/assets/"):
                return self._serve_asset(path[len("/assets/"):])
            if path.startswith("/cluster/"):
                cid = urllib.parse.unquote(path[len("/cluster/"):])
                return self._send(200, render_cluster(proj, cid))
            if path.startswith("/node/"):
                nid = urllib.parse.unquote(path[len("/node/"):])
                return self._send(200, render_node(proj, nid))
            return self._send(404, b"not found")

        def _serve_asset(self, name):
            # Only serve files under assets/review/ (no traversal).
            safe = Path(name).name
            f = _ASSETS_DIR / safe
            if not f.is_file():
                return self._send(404, b"asset not found")
            ctype = {
                ".css": "text/css; charset=utf-8",
                ".js": "application/javascript; charset=utf-8",
                ".svg": "image/svg+xml",
            }.get(f.suffix, "application/octet-stream")
            return self._send(200, f.read_bytes(), ctype)

        # --- POST ---
        def do_POST(self):
            path = urllib.parse.urlparse(self.path).path
            if not path.startswith("/api/verdict/"):
                return self._json(404, {"ok": False, "error": "not found"})
            node_id = urllib.parse.unquote(path[len("/api/verdict/"):])
            nodes = proj.nodes()
            if node_id not in nodes:
                return self._json(404, {"ok": False, "error": "unknown node"})
            length = int(self.headers.get("Content-Length", "0"))
            try:
                posted = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._json(400, {"ok": False, "error": "bad json"})

            verdict = posted.get("verdict")
            if verdict not in rm.VERDICTS:
                return self._json(400, {"ok": False,
                                        "error": f"verdict must be one of "
                                                 f"{list(rm.VERDICTS)}"})
            score = posted.get("score")
            try:
                score = None if score in (None, "") else int(score)
            except (TypeError, ValueError):
                return self._json(400, {"ok": False, "error": "score not an int"})

            from datetime import datetime, timezone
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            sidecar = proj.sidecar()
            try:
                updated = rm.apply_human_verdict(
                    sidecar, node_id, verdict, score,
                    posted.get("note", ""), posted.get("by", "reviewer"), now)
            except ValueError as exc:
                return self._json(400, {"ok": False, "error": str(exc)})
            proj.write_sidecar(updated)

            # Recompute and return the delta the client needs to repaint.
            state = rm.compute_state(nodes, updated)
            return self._json(200, {
                "ok": True,
                "node": node_id,
                "effective": rm.verdict_of(node_id, updated),
                "verdicts": state["verdicts"],
                "tainted": state["tainted"],
                "coverage": state["coverage"],
                "trust_frontier": state["trust_frontier"],
            })

    return Handler


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Local DAG review server (127.0.0.1)")
    ap.add_argument("--graph", type=Path, required=True, help="path to graph.json")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--open", action="store_true",
                    help="open the home screen in a browser")
    args = ap.parse_args(argv)

    graph_path = args.graph.resolve()
    if not graph_path.is_file():
        ap.error(f"graph.json not found: {graph_path}")
    proj = Project(graph_path)

    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(proj))
    url = f"http://127.0.0.1:{args.port}/"
    print(f"review server → {url}  (Ctrl-C to stop)")
    print(f"  graph:   {graph_path}")
    print(f"  sidecar: {proj.sidecar_path}")
    if not proj.blueprint_html.is_file():
        print(f"  note: blueprint not built at {proj.blueprint_html}\n"
              f"        node packets will show raw informal_content until "
              f"`review --view` builds it.")
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
