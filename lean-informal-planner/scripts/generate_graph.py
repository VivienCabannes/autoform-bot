#!/usr/bin/env python3
"""Generate an interactive HTML visualization from a plan.json file.

Usage:
    python generate_graph.py <path-to-plan.json>

Outputs plan_graph.html in the same directory as the input file.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

STATUS_COLORS = {
    "in-mathlib": "#16a34a",   # green
    "partial": "#d97706",      # amber
    "missing": "#dc2626",      # red
    "unchecked": "#94a3b8",    # slate gray
}

STATUS_LABELS = {
    "in-mathlib": "In Mathlib",
    "partial": "Partial",
    "missing": "Missing",
    "unchecked": "Unchecked",
}

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Formalization Plan — {title}</title>
<script src="https://unpkg.com/dagre@0.8.5/dist/dagre.min.js"></script>
<script src="https://unpkg.com/cytoscape@3.30.4/dist/cytoscape.min.js"></script>
<script src="https://unpkg.com/cytoscape-dagre@2.5.0/cytoscape-dagre.js"></script>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0f172a; color: #e2e8f0; display: flex; height: 100vh; }}

/* Summary bar */
#summary {{
    position: fixed; top: 0; left: 0; right: 0; z-index: 100;
    background: #1e293b; border-bottom: 1px solid #334155;
    padding: 8px 16px; display: flex; align-items: center; gap: 16px;
    font-size: 13px;
}}
#summary .title {{ font-weight: 600; font-size: 15px; color: #f8fafc; }}
.stat {{ display: flex; align-items: center; gap: 4px; }}
.stat .dot {{ width: 10px; height: 10px; border-radius: 50%; display: inline-block; }}

/* Filters */
#filters {{
    position: fixed; top: 44px; left: 0; z-index: 100;
    background: #1e293b; border-bottom: 1px solid #334155; border-right: 1px solid #334155;
    padding: 8px 12px; display: flex; gap: 12px; font-size: 12px;
}}
#filters label {{ display: flex; align-items: center; gap: 4px; cursor: pointer; }}
#filters input[type="checkbox"] {{ cursor: pointer; }}

/* Graph container */
#cy {{
    flex: 1; margin-top: 44px;
}}

/* Detail panel */
#detail {{
    width: 360px; min-width: 360px; margin-top: 44px;
    background: #1e293b; border-left: 1px solid #334155;
    overflow-y: auto; padding: 16px;
    display: none;
}}
#detail.visible {{ display: block; }}
#detail h2 {{ font-size: 16px; color: #f8fafc; margin-bottom: 4px; }}
#detail .kind {{ font-size: 12px; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 12px; }}
#detail .section {{ margin-bottom: 14px; }}
#detail .section-title {{ font-size: 11px; color: #64748b; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 4px; }}
#detail .section-body {{ font-size: 13px; line-height: 1.5; color: #cbd5e1; }}
#detail .status-badge {{
    display: inline-block; padding: 2px 8px; border-radius: 4px;
    font-size: 11px; font-weight: 600; color: #fff;
}}
#detail a {{ color: #60a5fa; text-decoration: none; }}
#detail a:hover {{ text-decoration: underline; }}
#detail .dep-list {{ list-style: none; padding: 0; }}
#detail .dep-list li {{ font-size: 13px; padding: 2px 0; color: #94a3b8; cursor: pointer; }}
#detail .dep-list li:hover {{ color: #60a5fa; }}
#detail .target-badge {{
    display: inline-block; padding: 1px 6px; border-radius: 3px;
    font-size: 10px; font-weight: 600; color: #0f172a; background: #38bdf8;
    margin-left: 6px;
}}
</style>
</head>
<body>

<div id="summary">
    <span class="title">{title}</span>
    <span class="stat"><span class="dot" style="background:{color_in_mathlib}"></span> {count_in_mathlib} In Mathlib</span>
    <span class="stat"><span class="dot" style="background:{color_partial}"></span> {count_partial} Partial</span>
    <span class="stat"><span class="dot" style="background:{color_missing}"></span> {count_missing} Missing</span>
    <span class="stat"><span class="dot" style="background:{color_unchecked}"></span> {count_unchecked} Unchecked</span>
    <span class="stat" style="margin-left:auto; color:#64748b;">{count_total} concepts</span>
</div>

<div id="filters">
    <label><input type="checkbox" class="filter-cb" data-status="in-mathlib" checked> In Mathlib</label>
    <label><input type="checkbox" class="filter-cb" data-status="partial" checked> Partial</label>
    <label><input type="checkbox" class="filter-cb" data-status="missing" checked> Missing</label>
    <label><input type="checkbox" class="filter-cb" data-status="unchecked" checked> Unchecked</label>
</div>

<div id="cy"></div>
<div id="detail"></div>

<script>
const PLAN = {plan_json};

const STATUS_COLORS = {status_colors_json};
const STATUS_LABELS = {status_labels_json};

// Build cytoscape elements
const elements = [];

PLAN.concepts.forEach(c => {{
    elements.push({{
        data: {{
            id: c.id,
            label: c.name,
            kind: c.kind,
            description: c.description || '',
            source_refs: c.source_refs || [],
            is_target: c.is_target || false,
            mathlib_status: c.mathlib_status || 'unchecked',
            mathlib_declarations: c.mathlib_declarations || [],
            mathlib_file: c.mathlib_file || '',
            mathlib_notes: c.mathlib_notes || '',
            depends_on: c.depends_on || [],
            statusColor: STATUS_COLORS[c.mathlib_status] || STATUS_COLORS['unchecked'],
        }}
    }});

    (c.depends_on || []).forEach(dep => {{
        elements.push({{
            data: {{ source: c.id, target: dep }}
        }});
    }});
}});

const cy = cytoscape({{
    container: document.getElementById('cy'),
    elements: elements,
    style: [
        {{
            selector: 'node',
            style: {{
                'label': 'data(label)',
                'background-color': 'data(statusColor)',
                'color': '#e2e8f0',
                'text-valign': 'bottom',
                'text-halign': 'center',
                'font-size': '10px',
                'text-margin-y': 4,
                'width': function(ele) {{ return ele.data('is_target') ? 28 : 20; }},
                'height': function(ele) {{ return ele.data('is_target') ? 28 : 20; }},
                'shape': function(ele) {{ return ele.data('is_target') ? 'round-rectangle' : 'ellipse'; }},
                'border-width': function(ele) {{ return ele.data('is_target') ? 2 : 0; }},
                'border-color': '#38bdf8',
                'text-wrap': 'ellipsis',
                'text-max-width': '120px',
            }}
        }},
        {{
            selector: 'edge',
            style: {{
                'width': 1.5,
                'line-color': '#475569',
                'target-arrow-color': '#475569',
                'target-arrow-shape': 'triangle',
                'curve-style': 'bezier',
                'arrow-scale': 0.8,
            }}
        }},
        {{
            selector: 'node:selected',
            style: {{
                'border-width': 3,
                'border-color': '#f8fafc',
            }}
        }},
        {{
            selector: '.hidden',
            style: {{
                'display': 'none',
            }}
        }},
    ],
    layout: {{
        name: 'dagre',
        rankDir: 'TB',
        nodeSep: 40,
        rankSep: 60,
        padding: 60,
    }},
}});

// Adjust for filter/summary bar
cy.on('layoutstop', () => {{
    cy.fit(undefined, 60);
}});

// Detail panel
const detailPanel = document.getElementById('detail');

function showDetail(data) {{
    const statusColor = STATUS_COLORS[data.mathlib_status] || STATUS_COLORS['unchecked'];
    const statusLabel = STATUS_LABELS[data.mathlib_status] || 'Unknown';

    let refs = '';
    if (data.source_refs && data.source_refs.length > 0) {{
        refs = data.source_refs.map(r => `${{r.file}}: ${{r.location}}`).join('<br>');
    }} else {{
        refs = '<em>No references</em>';
    }}

    let mathlibDecls = '';
    if (data.mathlib_declarations && data.mathlib_declarations.length > 0) {{
        mathlibDecls = data.mathlib_declarations.map(d => {{
            const url = data.mathlib_file
                ? `https://leanprover-community.github.io/mathlib4_docs/${{data.mathlib_file.replace('.lean', '.html').replace(/\\//g, '/')}}#${{d}}`
                : '#';
            return `<a href="${{url}}" target="_blank">${{d}}</a>`;
        }}).join(', ');
    }} else {{
        mathlibDecls = '<em>None</em>';
    }}

    let deps = '';
    if (data.depends_on && data.depends_on.length > 0) {{
        deps = '<ul class="dep-list">' +
            data.depends_on.map(id => {{
                const node = PLAN.concepts.find(c => c.id === id);
                const name = node ? node.name : id;
                return `<li onclick="selectNode('${{id}}')">${{id}}: ${{name}}</li>`;
            }}).join('') + '</ul>';
    }} else {{
        deps = '<em>No dependencies (root node)</em>';
    }}

    // Find dependents (concepts that depend on this one)
    const dependents = PLAN.concepts.filter(c => (c.depends_on || []).includes(data.id));
    let dependentsList = '';
    if (dependents.length > 0) {{
        dependentsList = '<ul class="dep-list">' +
            dependents.map(c => `<li onclick="selectNode('${{c.id}}')">${{c.id}}: ${{c.name}}</li>`).join('') +
            '</ul>';
    }} else {{
        dependentsList = '<em>No dependents (leaf node)</em>';
    }}

    detailPanel.innerHTML = `
        <h2>${{data.label}}${{data.is_target ? '<span class="target-badge">TARGET</span>' : ''}}</h2>
        <div class="kind">${{data.kind}} · ${{data.id}}</div>

        <div class="section">
            <div class="section-title">Status</div>
            <div class="section-body"><span class="status-badge" style="background:${{statusColor}}">${{statusLabel}}</span></div>
        </div>

        <div class="section">
            <div class="section-title">Description</div>
            <div class="section-body">${{data.description || '<em>No description</em>'}}</div>
        </div>

        <div class="section">
            <div class="section-title">Source References</div>
            <div class="section-body">${{refs}}</div>
        </div>

        <div class="section">
            <div class="section-title">Mathlib Declarations</div>
            <div class="section-body">${{mathlibDecls}}</div>
        </div>

        ${{data.mathlib_notes ? `
        <div class="section">
            <div class="section-title">Mathlib Notes</div>
            <div class="section-body">${{data.mathlib_notes}}</div>
        </div>` : ''}}

        <div class="section">
            <div class="section-title">Dependencies (${{data.depends_on ? data.depends_on.length : 0}})</div>
            <div class="section-body">${{deps}}</div>
        </div>

        <div class="section">
            <div class="section-title">Dependents (${{dependents.length}})</div>
            <div class="section-body">${{dependentsList}}</div>
        </div>
    `;
    detailPanel.classList.add('visible');
}}

function selectNode(id) {{
    const node = cy.getElementById(id);
    if (node.length > 0) {{
        cy.elements().unselect();
        node.select();
        cy.animate({{ center: {{ eles: node }}, duration: 300 }});
        showDetail(node.data());
    }}
}}

// Make selectNode available globally for onclick handlers
window.selectNode = selectNode;

cy.on('tap', 'node', function(evt) {{
    showDetail(evt.target.data());
}});

cy.on('tap', function(evt) {{
    if (evt.target === cy) {{
        detailPanel.classList.remove('visible');
    }}
}});

// Filtering
document.querySelectorAll('.filter-cb').forEach(cb => {{
    cb.addEventListener('change', () => {{
        const activeStatuses = new Set();
        document.querySelectorAll('.filter-cb:checked').forEach(c => activeStatuses.add(c.dataset.status));

        cy.nodes().forEach(node => {{
            if (activeStatuses.has(node.data('mathlib_status'))) {{
                node.removeClass('hidden');
            }} else {{
                node.addClass('hidden');
            }}
        }});

        // Also hide edges connected to hidden nodes
        cy.edges().forEach(edge => {{
            const src = edge.source();
            const tgt = edge.target();
            if (src.hasClass('hidden') || tgt.hasClass('hidden')) {{
                edge.addClass('hidden');
            }} else {{
                edge.removeClass('hidden');
            }}
        }});
    }});
}});
</script>
</body>
</html>"""


def generate_html(plan_path: str | Path) -> Path:
    """Generate an interactive HTML visualization from a plan.json file."""
    plan_path = Path(plan_path)
    if not plan_path.exists():
        print(f"Error: {plan_path} not found", file=sys.stderr)
        sys.exit(1)

    with open(plan_path) as f:
        plan = json.load(f)

    # Compute summary counts
    summary = plan.get("summary", {})
    count_total = summary.get("total", len(plan.get("concepts", [])))
    count_in_mathlib = summary.get("in_mathlib", 0)
    count_partial = summary.get("partial", 0)
    count_missing = summary.get("missing", 0)
    count_unchecked = summary.get("unchecked", 0)

    # If summary is missing/stale, recompute from concepts
    if count_in_mathlib + count_partial + count_missing + count_unchecked != count_total:
        concepts = plan.get("concepts", [])
        count_total = len(concepts)
        count_in_mathlib = sum(1 for c in concepts if c.get("mathlib_status") == "in-mathlib")
        count_partial = sum(1 for c in concepts if c.get("mathlib_status") == "partial")
        count_missing = sum(1 for c in concepts if c.get("mathlib_status") == "missing")
        count_unchecked = sum(1 for c in concepts if c.get("mathlib_status", "unchecked") == "unchecked")

    # Build title from sources
    sources = plan.get("metadata", {}).get("sources", [])
    if sources:
        title = ", ".join(s.get("title", s.get("file", "Unknown")) for s in sources)
    else:
        title = "Formalization Plan"

    html = HTML_TEMPLATE.format(
        title=title,
        plan_json=json.dumps(plan, indent=2),
        status_colors_json=json.dumps(STATUS_COLORS),
        status_labels_json=json.dumps(STATUS_LABELS),
        count_total=count_total,
        count_in_mathlib=count_in_mathlib,
        count_partial=count_partial,
        count_missing=count_missing,
        count_unchecked=count_unchecked,
        color_in_mathlib=STATUS_COLORS["in-mathlib"],
        color_partial=STATUS_COLORS["partial"],
        color_missing=STATUS_COLORS["missing"],
        color_unchecked=STATUS_COLORS["unchecked"],
    )

    output_path = plan_path.parent / "plan_graph.html"
    output_path.write_text(html)
    print(f"Generated: {output_path}")
    return output_path


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <path-to-plan.json>", file=sys.stderr)
        sys.exit(1)
    generate_html(sys.argv[1])
