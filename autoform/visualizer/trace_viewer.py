# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Standalone AgentTrace viewer — generates a self-contained HTML file.

Usage:
    python -m autoform.visualizer.trace_viewer path/to/agent_trace.json
    # -> path/to/agent_trace.html
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any

from core.message_extract import extract_assistant, extract_text_content, extract_tool_results


# ── Formatting helpers ──────────────────────────────────────────────


def _fmt_duration(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{int(m)}m {s:.0f}s"
    h, m = divmod(int(m), 60)
    return f"{h}h {int(m)}m"


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _fmt_cost(cost: float) -> str:
    if cost == 0:
        return "$0.00"
    if cost < 0.01:
        return f"${cost:.4f}"
    return f"${cost:.2f}"


def _fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S")


def _fmt_ms(ms: float) -> str:
    if ms < 1000:
        return f"{ms:.0f}ms"
    return f"{ms / 1000:.2f}s"


def _status_class(status: str) -> str:
    if status == "success":
        return "badge-success"
    if status in ("failed", "error"):
        return "badge-fail"
    return "badge-running"


# ── CSS ─────────────────────────────────────────────────────────────

CSS = """\
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
  background:#f5f6f8;color:#1a1a1a;line-height:1.5}
.container{max-width:1200px;margin:0 auto;padding:0 24px 48px}

/* header */
.header{background:#1a1a2e;color:#fff;padding:24px 0;margin-bottom:0}
.header .container{display:flex;align-items:center;gap:16px;flex-wrap:wrap;padding-bottom:0}
.header h1{font-size:1.5rem;font-weight:600}
.header .meta{font-size:.85rem;color:#a0a0b0}
.badge{display:inline-block;padding:2px 10px;border-radius:12px;font-size:.8rem;
  font-weight:600;text-transform:uppercase}
.badge-success{background:#28a745;color:#fff}
.badge-fail{background:#dc3545;color:#fff}
.badge-running{background:#ffc107;color:#333}

/* nav */
nav{background:#fff;border-bottom:1px solid #e0e0e0;position:sticky;top:0;z-index:100}
nav .container{display:flex;gap:0;overflow-x:auto;padding-bottom:0}
nav a{padding:12px 20px;text-decoration:none;color:#555;font-size:.9rem;
  font-weight:500;border-bottom:2px solid transparent;white-space:nowrap}
nav a:hover{color:#1a1a2e;border-bottom-color:#1a1a2e}

/* sections */
section{margin-bottom:32px}
section h2{font-size:1.25rem;font-weight:600;margin-bottom:16px;padding-top:16px}

/* stat cards */
.stats-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:16px}
.stat-card{background:#fff;border-radius:8px;padding:20px;text-align:center;
  box-shadow:0 1px 3px rgba(0,0,0,.08)}
.stat-card .value{font-size:1.6rem;font-weight:700;color:#1a1a2e}
.stat-card .label{font-size:.8rem;color:#888;margin-top:4px;text-transform:uppercase;
  letter-spacing:.5px}
.stat-card .sub{font-size:.75rem;color:#aaa;margin-top:2px}

/* tables */
.table-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;background:#fff;border-radius:8px;
  overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.08);font-size:.88rem}
th{background:#f0f0f5;text-align:left;padding:10px 14px;font-weight:600;
  color:#555;font-size:.8rem;text-transform:uppercase;letter-spacing:.3px}
td{padding:8px 14px;border-top:1px solid #f0f0f0}
tr:hover td{background:#fafbfc}
tfoot td{background:#f8f8fc;font-weight:600;border-top:2px solid #e0e0e0}
.num{text-align:right;font-variant-numeric:tabular-nums}

/* time breakdown */
.time-bar-container{background:#fff;border-radius:8px;padding:24px;
  box-shadow:0 1px 3px rgba(0,0,0,.08)}
.time-bar{display:flex;height:36px;border-radius:6px;overflow:hidden;background:#e0e0e0}
.time-bar .seg{display:flex;align-items:center;justify-content:center;
  font-size:.75rem;font-weight:600;color:#fff;min-width:2px}
.seg-llm{background:#5b6abf}
.seg-tool{background:#e67e22}
.seg-overhead{background:#95a5a6}
.time-legend{display:flex;gap:24px;margin-top:12px;font-size:.82rem;flex-wrap:wrap}
.time-legend span::before{content:'';display:inline-block;width:12px;height:12px;
  border-radius:3px;margin-right:6px;vertical-align:-1px}
.time-legend .l-llm::before{background:#5b6abf}
.time-legend .l-tool::before{background:#e67e22}
.time-legend .l-overhead::before{background:#95a5a6}

/* conversation */
.msg{margin-bottom:12px;border-radius:8px;padding:14px 18px}
.msg-system{background:#e9ecef;color:#555}
.msg-user{background:#e3f2fd;border-left:4px solid #1976d2}
.msg-assistant{background:#e8f5e9;border-left:4px solid #388e3c}
.msg-role{font-size:.75rem;font-weight:700;text-transform:uppercase;color:#888;
  margin-bottom:6px;letter-spacing:.5px}
.msg-content{white-space:pre-wrap;word-break:break-word;
  font-family:'SF Mono','Fira Code',Consolas,monospace;font-size:.82rem;line-height:1.6}
.thinking-block{background:#fff8e1;border-left:3px solid #f9a825;padding:10px 14px;
  margin:8px 0;border-radius:4px}
.thinking-header{cursor:pointer;font-size:.78rem;font-weight:600;color:#f57f17;
  user-select:none}
.thinking-content{margin-top:8px}

/* tool cards */
.tool-card{background:#f3e5f5;border-radius:6px;padding:10px 14px;margin:8px 0;
  border-left:3px solid #8e24aa}
.tool-card-header{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.tool-card-name{font-weight:700;color:#6a1b9a;font-size:.88rem}
.tool-card-meta{font-size:.75rem;color:#888}
.tool-badge{font-size:.7rem;padding:1px 7px;border-radius:8px}
.tool-badge-ok{background:#c8e6c9;color:#2e7d32}
.tool-badge-fail{background:#ffcdd2;color:#c62828}
.tool-result{background:#fafafa;border-radius:4px;padding:10px 14px;margin:6px 0 0;
  border:1px solid #e0e0e0}

/* collapsible */
.collapsible-toggle{cursor:pointer;user-select:none}
.collapsible-toggle::before{content:'\u25b6 ';font-size:.7rem;display:inline-block;
  transition:transform .15s}
.collapsible-toggle.open::before{transform:rotate(90deg)}
.collapsible-body{display:none}
.collapsible-body.open{display:block}

/* error */
.error-box{background:#fff5f5;border:1px solid #fc8181;border-radius:8px;
  padding:16px;margin-bottom:24px;color:#c53030}
pre{background:#f8f8f8;border:1px solid #e8e8e8;border-radius:4px;padding:8px;
  overflow:auto;max-height:300px}
"""

# ── JS ──────────────────────────────────────────────────────────────

JS = """\
function toggle(id){
  var b=document.getElementById(id);
  var t=document.querySelector('[data-target="'+id+'"]');
  if(b){b.classList.toggle('open');if(t)t.classList.toggle('open')}
}
"""

# ── Unique ID generator ────────────────────────────────────────────

_counter = 0


def _uid() -> str:
    global _counter
    _counter += 1
    return f"c{_counter}"


# ── Collapsible helper ─────────────────────────────────────────────


def _collapsible(label: str, content_html: str, *, open_: bool = False) -> str:
    uid = _uid()
    cls = " open" if open_ else ""
    return (
        f'<span class="collapsible-toggle{cls}" data-target="{uid}" '
        f"onclick=\"toggle('{uid}')\">{escape(label)}</span>"
        f'<div id="{uid}" class="collapsible-body{cls}">{content_html}</div>'
    )


# ── Section renderers ──────────────────────────────────────────────


def _render_header(t: dict[str, Any]) -> str:
    agent_id = escape(str(t.get("agent_id", t.get("trace_id", "unknown"))))
    status = t.get("final_status", "unknown")
    badge_cls = _status_class(status)

    started = t.get("started_at", 0)
    ended = t.get("ended_at")
    duration = (ended - started) if ended and started else 0

    models = {c.get("model", "") for c in t.get("llm_calls", [])}
    model_str = ", ".join(sorted(m for m in models if m)) or "\u2014"

    trace_id = escape(str(t.get("trace_id", "")))
    meta_parts = [
        f"Trace: {trace_id}",
        f"Started: {_fmt_ts(started)}" if started else "",
        f"Duration: {_fmt_duration(duration)}" if duration else "",
        f"Model: {escape(model_str)}",
    ]
    meta = " \u00b7 ".join(p for p in meta_parts if p)

    error_html = ""
    if t.get("error"):
        error_html = f'<div class="error-box"><strong>Error:</strong> {escape(str(t["error"]))}</div>'

    return (
        f'<div class="header"><div class="container">'
        f"<h1>{agent_id}</h1>"
        f'<span class="badge {badge_cls}">{escape(status)}</span>'
        f'<div class="meta">{meta}</div>'
        f"</div></div>"
        f'<nav><div class="container">'
        f'<a href="#stats">Stats</a>'
        f'<a href="#llm-calls">LLM Calls</a>'
        f'<a href="#tool-use">Tool Use</a>'
        f'<a href="#time">Time</a>'
        f'<a href="#conversation">Conversation</a>'
        f"</div></nav>"
        f'<div class="container">{error_html}'
    )


def _render_stats_cards(t: dict[str, Any]) -> str:
    summary = t.get("summary", {})
    started = t.get("started_at", 0)
    ended = t.get("ended_at")
    duration = (ended - started) if ended and started else 0

    successful_tools = summary.get("successful_tool_calls", 0)
    total_tools = summary.get("num_tool_calls", 0)
    input_tok = summary.get("total_input_tokens", 0)
    output_tok = summary.get("total_output_tokens", 0)

    cards = [
        (_fmt_duration(duration), "Duration", ""),
        (str(summary.get("total_turns", 0)), "Turns", ""),
        (str(summary.get("num_llm_calls", 0)), "LLM Calls", ""),
        (f"{successful_tools}/{total_tools}", "Tool Calls", "success / total"),
        (
            _fmt_tokens(input_tok + output_tok),
            "Tokens",
            f"{_fmt_tokens(input_tok)} in + {_fmt_tokens(output_tok)} out",
        ),
        (_fmt_cost(summary.get("total_cost_usd", 0)), "Cost", ""),
    ]

    cards_html = "".join(
        f'<div class="stat-card"><div class="value">{v}</div>'
        f'<div class="label">{lbl}</div>' + (f'<div class="sub">{sub}</div>' if sub else "") + "</div>"
        for v, lbl, sub in cards
    )
    return f'<section id="stats"><h2>Stats</h2><div class="stats-grid">{cards_html}</div></section>'


def _render_llm_table(t: dict[str, Any]) -> str:
    llm_calls = t.get("llm_calls", [])
    if not llm_calls:
        return '<section id="llm-calls"><h2>LLM Calls</h2><p>No LLM calls recorded.</p></section>'

    started_at = t.get("started_at", 0)
    rows: list[str] = []
    for i, c in enumerate(llm_calls, 1):
        rel = c.get("timestamp", 0) - started_at
        rows.append(
            f"<tr>"
            f'<td class="num">{i}</td>'
            f"<td>{escape(c.get('model', ''))}</td>"
            f'<td class="num">+{_fmt_duration(rel)}</td>'
            f'<td class="num">{_fmt_ms(c.get("latency_ms", 0))}</td>'
            f'<td class="num">{_fmt_tokens(c.get("usage", {}).get("input_tokens", 0))}</td>'
            f'<td class="num">{_fmt_tokens(c.get("usage", {}).get("output_tokens", 0))}</td>'
            f'<td class="num">{_fmt_tokens(c.get("usage", {}).get("total_tokens", 0))}</td>'
            f'<td class="num">{_fmt_cost(c.get("cost_usd") or 0)}</td>'
            f"</tr>"
        )

    tot_lat = sum(c.get("latency_ms", 0) for c in llm_calls)
    tot_in = sum(c.get("usage", {}).get("input_tokens", 0) for c in llm_calls)
    tot_out = sum(c.get("usage", {}).get("output_tokens", 0) for c in llm_calls)
    tot_tok = sum(c.get("usage", {}).get("total_tokens", 0) for c in llm_calls)
    tot_cost = sum((c.get("cost_usd") or 0) for c in llm_calls)

    return (
        f'<section id="llm-calls"><h2>LLM Calls</h2><div class="table-wrap"><table>'
        f"<thead><tr>"
        f'<th class="num">#</th><th>Model</th><th class="num">Time</th>'
        f'<th class="num">Latency</th><th class="num">In</th><th class="num">Out</th>'
        f'<th class="num">Total</th><th class="num">Cost</th>'
        f"</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        f"<tfoot><tr>"
        f"<td></td><td>Totals ({len(llm_calls)} calls)</td><td></td>"
        f'<td class="num">{_fmt_ms(tot_lat)}</td>'
        f'<td class="num">{_fmt_tokens(tot_in)}</td>'
        f'<td class="num">{_fmt_tokens(tot_out)}</td>'
        f'<td class="num">{_fmt_tokens(tot_tok)}</td>'
        f'<td class="num">{_fmt_cost(tot_cost)}</td>'
        f"</tr></tfoot></table></div></section>"
    )


def _render_tool_stats(t: dict[str, Any]) -> str:
    tool_calls = t.get("tool_calls", [])
    if not tool_calls:
        return '<section id="tool-use"><h2>Tool Use</h2><p>No tool calls recorded.</p></section>'

    # Aggregate per-tool stats
    by_tool: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for tc in tool_calls:
        by_tool[tc.get("tool_name", "unknown")].append(tc)

    agg_rows: list[str] = []
    for name in sorted(by_tool):
        calls = by_tool[name]
        count = len(calls)
        ok = sum(1 for c in calls if c.get("success"))
        fail = count - ok
        rate = (ok / count * 100) if count else 0
        durations = [c.get("duration_ms") or 0 for c in calls]
        mn = min(durations) if durations else 0
        mx = max(durations) if durations else 0
        avg = sum(durations) / len(durations) if durations else 0
        tot = sum(durations)
        agg_rows.append(
            f"<tr>"
            f"<td><strong>{escape(name)}</strong></td>"
            f'<td class="num">{count}</td>'
            f'<td class="num">{ok}/{fail}</td>'
            f'<td class="num">{rate:.0f}%</td>'
            f'<td class="num">{_fmt_ms(mn)}</td>'
            f'<td class="num">{_fmt_ms(avg)}</td>'
            f'<td class="num">{_fmt_ms(mx)}</td>'
            f'<td class="num">{_fmt_ms(tot)}</td>'
            f"</tr>"
        )

    # Individual tool calls
    detail_rows: list[str] = []
    for i, tc in enumerate(tool_calls, 1):
        ok = tc.get("success", False)
        badge = (
            '<span class="tool-badge tool-badge-ok">OK</span>'
            if ok
            else '<span class="tool-badge tool-badge-fail">FAIL</span>'
        )
        dur = _fmt_ms(tc.get("duration_ms") or 0)

        args_json = json.dumps(tc.get("arguments", {}), indent=2)
        result_raw = tc.get("result") or ""
        try:
            result_str = json.dumps(json.loads(result_raw), indent=2)
        except (json.JSONDecodeError, TypeError):
            result_str = result_raw

        args_col = _collapsible("Arguments", f"<pre>{escape(args_json)}</pre>")
        result_col = _collapsible("Result", f"<pre>{escape(result_str)}</pre>")

        error_html = ""
        if tc.get("error"):
            error_html = f'<div style="color:#c62828;font-size:.8rem;margin-top:4px">{escape(str(tc["error"]))}</div>'

        detail_rows.append(
            f"<tr>"
            f'<td class="num">{i}</td>'
            f"<td><strong>{escape(tc.get('tool_name', ''))}</strong></td>"
            f"<td>{badge}</td>"
            f'<td class="num">{dur}</td>'
            f"<td>{args_col} {result_col}{error_html}</td>"
            f"</tr>"
        )

    return (
        f'<section id="tool-use"><h2>Tool Use</h2>'
        f'<h3 style="font-size:1rem;margin:12px 0 8px">Aggregate Stats</h3>'
        f'<div class="table-wrap"><table>'
        f"<thead><tr>"
        f"<th>Tool</th>"
        f'<th class="num">Count</th>'
        f'<th class="num">Ok/Fail</th>'
        f'<th class="num">Rate</th>'
        f'<th class="num">Min</th>'
        f'<th class="num">Avg</th>'
        f'<th class="num">Max</th>'
        f'<th class="num">Total</th>'
        f"</tr></thead>"
        f"<tbody>{''.join(agg_rows)}</tbody>"
        f"</table></div>"
        f'<h3 style="font-size:1rem;margin:24px 0 8px">Individual Calls</h3>'
        f'<div class="table-wrap"><table>'
        f"<thead><tr>"
        f'<th class="num">#</th><th>Tool</th><th>Status</th>'
        f'<th class="num">Duration</th><th>Details</th>'
        f"</tr></thead>"
        f"<tbody>{''.join(detail_rows)}</tbody>"
        f"</table></div></section>"
    )


def _render_time_breakdown(t: dict[str, Any]) -> str:
    started = t.get("started_at", 0)
    ended = t.get("ended_at")
    total_ms = ((ended - started) * 1000) if ended and started else 0
    if total_ms <= 0:
        return '<section id="time"><h2>Time Breakdown</h2><p>No timing data.</p></section>'

    llm_ms = sum(c.get("latency_ms", 0) for c in t.get("llm_calls", []))
    tool_ms = sum((c.get("duration_ms") or 0) for c in t.get("tool_calls", []))
    overhead_ms = max(0, total_ms - llm_ms - tool_ms)

    llm_pct = llm_ms / total_ms * 100
    tool_pct = tool_ms / total_ms * 100
    overhead_pct = overhead_ms / total_ms * 100

    def seg(cls: str, pct: float, ms: float) -> str:
        if pct < 1:
            return ""
        label = _fmt_ms(ms) if pct > 8 else ""
        return f'<div class="seg {cls}" style="width:{pct:.1f}%">{label}</div>'

    return (
        f'<section id="time"><h2>Time Breakdown</h2>'
        f'<div class="time-bar-container"><div class="time-bar">'
        f"{seg('seg-llm', llm_pct, llm_ms)}"
        f"{seg('seg-tool', tool_pct, tool_ms)}"
        f"{seg('seg-overhead', overhead_pct, overhead_ms)}"
        f"</div>"
        f'<div class="time-legend">'
        f'<span class="l-llm">LLM: {_fmt_ms(llm_ms)} ({llm_pct:.1f}%)</span>'
        f'<span class="l-tool">Tools: {_fmt_ms(tool_ms)} ({tool_pct:.1f}%)</span>'
        f'<span class="l-overhead">Overhead: {_fmt_ms(overhead_ms)} ({overhead_pct:.1f}%)</span>'
        f"</div></div></section>"
    )


# ── Conversation rendering ─────────────────────────────────────────


def _render_assistant_content(text: str, thinking: str = "") -> str:
    html_parts: list[str] = []
    if thinking.strip():
        html_parts.append(
            '<div class="thinking-block">'
            + _collapsible(
                "Thinking",
                f'<div class="thinking-content msg-content">{escape(thinking.strip())}</div>',
            )
            + "</div>"
        )
    if text.strip():
        html_parts.append(f'<div class="msg-content">{escape(text.strip())}</div>')
    return "\n".join(html_parts)


def _render_conversation(t: dict[str, Any]) -> str:
    messages = t.get("messages", [])
    if not messages:
        return '<section id="conversation"><h2>Conversation</h2><p>No messages.</p></section>'

    # Lookup: tool_call_id -> (result_text, is_error) — handles OpenAI and Anthropic formats
    tool_results: dict[str, tuple[str, bool]] = extract_tool_results(messages)

    # Lookup: call_id -> ToolCallRecord (for timing/status)
    tool_records: dict[str, dict[str, Any]] = {}
    for tc in t.get("tool_calls", []):
        tool_records[tc.get("call_id", "")] = tc

    rendered_tool_ids: set[str] = set()
    msg_htmls: list[str] = []

    for msg in messages:
        role = msg.get("role", "")

        if role == "tool":
            tcid = msg.get("tool_call_id", "")
            if tcid in rendered_tool_ids:
                continue
            # Orphaned tool result (OpenAI format, not matched to any tool_use)
            content = extract_text_content(msg)
            msg_htmls.append(
                '<div class="msg msg-system">'
                '<div class="msg-role">Tool Result</div>'
                + _collapsible(
                    "Show result",
                    f'<div class="msg-content">{escape(content)}</div>',
                )
                + "</div>"
            )
            continue

        if role == "system":
            content = extract_text_content(msg)
            msg_htmls.append(
                '<div class="msg msg-system">'
                '<div class="msg-role">System</div>'
                + _collapsible(
                    "Show system prompt",
                    f'<div class="msg-content">{escape(content)}</div>',
                )
                + "</div>"
            )
            continue

        if role == "user":
            content = extract_text_content(msg)
            if not content.strip():
                # Pure tool-result message (Anthropic format) — rendered inline in tool cards
                continue
            msg_htmls.append(
                f'<div class="msg msg-user">'
                f'<div class="msg-role">User</div>'
                f'<div class="msg-content">{escape(content)}</div>'
                f"</div>"
            )
            continue

        if role in ("assistant", "model"):
            extracted = extract_assistant(msg)
            inner = _render_assistant_content(extracted.text, extracted.thinking)

            for tc in extracted.tool_calls:
                tc_id = tc.id
                tool_name = tc.name
                args_str = json.dumps(tc.arguments, indent=2)

                # Timing from trace records
                record = tool_records.get(tc_id, {})
                duration = record.get("duration_ms")
                success = record.get("success")

                dur_str = _fmt_ms(duration) if duration is not None else ""
                if success is True:
                    sbadge = '<span class="tool-badge tool-badge-ok">OK</span>'
                elif success is False:
                    sbadge = '<span class="tool-badge tool-badge-fail">FAIL</span>'
                else:
                    sbadge = ""

                inner += (
                    f'<div class="tool-card">'
                    f'<div class="tool-card-header">'
                    f'<span class="tool-card-name">{escape(tool_name)}</span>'
                    f"{sbadge}"
                    f'<span class="tool-card-meta">{dur_str}</span>'
                    f"</div>"
                    f'<div style="margin-top:6px">'
                    + _collapsible(
                        "Arguments",
                        f"<pre>{escape(args_str)}</pre>",
                    )
                    + "</div>"
                )

                # Inline tool result
                if tc_id in tool_results:
                    rendered_tool_ids.add(tc_id)
                    res_content, is_error = tool_results[tc_id]
                    try:
                        res_content = json.dumps(json.loads(res_content), indent=2)
                    except (json.JSONDecodeError, TypeError):
                        pass
                    result_badge = '<span class="tool-badge tool-badge-fail">error</span>' if is_error else ""
                    inner += (
                        '<div class="tool-result">'
                        + result_badge
                        + _collapsible(
                            "Result",
                            f"<pre>{escape(res_content)}</pre>",
                        )
                        + "</div>"
                    )

                inner += "</div>"  # close tool-card

            msg_htmls.append(f'<div class="msg msg-assistant"><div class="msg-role">Assistant</div>{inner}</div>')
            continue

    return f'<section id="conversation"><h2>Conversation</h2>{"".join(msg_htmls)}</section>'


# ── Main generator ──────────────────────────────────────────────────


def generate_html(trace_data: dict[str, Any]) -> str:
    global _counter
    _counter = 0

    agent_id = trace_data.get("agent_id", trace_data.get("trace_id", "trace"))

    return (
        f"<!DOCTYPE html><html lang='en'><head>"
        f"<meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{escape(str(agent_id))} \u2014 Agent Trace</title>"
        f"<style>{CSS}</style></head><body>"
        f"{_render_header(trace_data)}"
        f"{_render_stats_cards(trace_data)}"
        f"{_render_llm_table(trace_data)}"
        f"{_render_tool_stats(trace_data)}"
        f"{_render_time_breakdown(trace_data)}"
        f"{_render_conversation(trace_data)}"
        f"</div>"  # close .container from header
        f"<script>{JS}</script></body></html>"
    )


# ── CLI entry point ─────────────────────────────────────────────────


def _serve(html: str, port: int) -> None:
    from flask import Flask

    app = Flask(__name__)

    @app.route("/")
    def index():  # type: ignore[reportUnusedFunction]
        return html

    print(f"Serving on http://localhost:{port}")
    app.run(port=port)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Generate a self-contained HTML viewer for an AgentTrace JSON.")
    parser.add_argument("trace_json", type=Path, help="Path to agent trace JSON")
    parser.add_argument(
        "output_html", type=Path, nargs="?", default=None, help="Output HTML path (default: same dir, .html extension)"
    )
    parser.add_argument(
        "--serve", type=int, metavar="PORT", nargs="?", const=8000, help="Start HTTP server on PORT (default 8000)"
    )
    args = parser.parse_args()

    trace_path: Path = args.trace_json
    if not trace_path.exists():
        parser.error(f"{trace_path} not found")

    with open(trace_path) as f:
        trace_data = json.load(f)

    html = generate_html(trace_data)

    if args.serve is not None:
        _serve(html, args.serve)
    else:
        out_path = args.output_html or trace_path.with_suffix(".html")
        out_path.write_text(html)
        print(f"Wrote {out_path} ({len(html):,} bytes)")


if __name__ == "__main__":
    main()
