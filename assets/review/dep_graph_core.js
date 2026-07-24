/* dep_graph_core.js — the ONE dependency-graph renderer, shared by both viewers:
 *   • the leanblueprint dep-graph page  (templates/dep_graph.html → dep_graph_document.html)
 *   • the live review surface           (assets/review/review.js)
 *
 * Neither viewer reimplements d3-graphviz anymore: both call DepGraphCore.renderDot
 * (and, optionally, DepGraphCore.tierToggle). Each keeps its own DOT *source* (the
 * blueprint reads baked tier_dots.js; the review surface fetches /api/dot, recolored)
 * and its own *interactions* (blueprint = per-statement modals; review = packets,
 * taint, dispatch). Only the rendering + tier-toggle scaffold is shared.
 *
 * The host page must have already loaded d3 + d3-graphviz (the blueprint bundles them
 * under js/; the review surface lazy-loads them from a CDN), so this module assumes
 * window.d3.select(...).graphviz is available when renderDot is called.
 */
(function (global) {
  "use strict";

  /* renderDot(mount, dot, opts) — render a DOT string into `mount` via d3-graphviz.
   *
   * Renders into a FRESH child "stage" element on every call: a *reused* graphviz
   * instance throws when the graph changes structure (e.g. a tier unroll that adds a
   * `subgraph cluster_…`), so a brand-new element — carrying no stale renderer state —
   * makes every (re)render a clean first-render. The prior graph stays visible until
   * the new one settles, then the old stages are dropped and onSettle(mount) runs.
   * Settles exactly once. d3-graphviz's "end" event is AUTHORITATIVE; the timeout
   * is only a fallback for builds where "end" never fires — and before settling it
   * checks that an <svg> actually landed in the stage. A big graph still laying
   * out past the timeout re-arms the check with backoff (up to ~30s) instead of
   * permanently settling early, which would wire interactivity onto nothing and
   * turn the real "end" into a no-op. A render throw removes the stage and calls
   * onError.
   *
   * opts:
   *   useWorker  — pass to .graphviz({useWorker}) (default false; the safe path)
   *   fit        — .fit(true) unless explicitly false
   *   stageClass — class on the fresh stage div (default "dg-stage"); callers with
   *                their own stage CSS (e.g. the review surface's "rv-graph-stage")
   *                pass it so their styling + drop-prior-stages logic still apply
   *   timeout    — initial fallback-check delay in ms (default 700)
   *   onSettle(mount) — run after the new graph settles (decorate / wire modals …)
   *   onError(mount, err) — run if the render throws (offline fallback …)
   */
  function renderDot(mount, dot, opts) {
    opts = opts || {};
    if (!mount) return;
    var stageClass = opts.stageClass || "dg-stage";
    var stage = document.createElement("div");
    stage.className = stageClass;
    stage.style.cssText = "width:100%;height:100%;";
    mount.appendChild(stage);

    var settled = false;
    function settle() {
      if (settled) return;
      settled = true;
      var stages = mount.querySelectorAll("." + stageClass);
      for (var i = 0; i < stages.length; i++) {
        if (stages[i] !== stage) stages[i].remove();
      }
      if (opts.onSettle) opts.onSettle(mount);
    }

    try {
      var gv = global.d3.select(stage).graphviz({ useWorker: !!opts.useWorker })
        .fit(opts.fit !== false);
      gv.renderDot(dot);
      gv.on("end", settle);                     // authoritative: the render finished

      // Fallback for builds where "end" never fires: after `timeout`, settle ONLY
      // if the SVG actually rendered. Otherwise the render is still in flight (big
      // graphs routinely lay out slower than 700ms) — re-arm with backoff up to a
      // generous ceiling rather than settling early (that would run onSettle on an
      // empty stage and make the real "end" a no-op). At the ceiling, settle
      // regardless, as the last-resort backstop the old timeout was.
      var delay = opts.timeout || 700;
      var CEILING = 30000;
      var waited = 0;
      function fallbackCheck() {
        if (settled) return;                    // "end" already settled — done
        if (stage.querySelector("svg") || waited >= CEILING) {
          settle();
          return;
        }
        waited += delay;
        delay = Math.min(delay * 2, 5000);      // backoff: 0.7s, 1.4s, 2.8s, 5s, 5s…
        setTimeout(fallbackCheck, delay);
      }
      setTimeout(fallbackCheck, delay);
    } catch (e) {
      settled = true;
      stage.remove();
      if (opts.onError) opts.onError(mount, e);
    }
  }

  /* tierToggle(container, cfg) — render a <select> of the present tiers into
   * `container` and call cfg.onSelect(tier:Number) on change. Returns the <select>,
   * or null if there are fewer than two tiers (nothing to toggle).
   *
   * cfg: { tiers:[Number], current:Number, labels:{n:String}, onSelect:fn(n) }
   */
  function tierToggle(container, cfg) {
    cfg = cfg || {};
    var tiers = cfg.tiers || [];
    var labels = cfg.labels || {};
    if (!container || tiers.length < 2) return null;
    var sel = document.createElement("select");
    sel.className = "dg-tier-select";
    tiers.forEach(function (t) {
      var o = document.createElement("option");
      o.value = String(t);
      o.textContent = labels[t] || ("tier " + t);
      if (String(t) === String(cfg.current)) o.selected = true;
      sel.appendChild(o);
    });
    sel.addEventListener("change", function () {
      if (cfg.onSelect) cfg.onSelect(Number(sel.value));
    });
    container.appendChild(sel);
    return sel;
  }

  global.DepGraphCore = { renderDot: renderDot, tierToggle: tierToggle };
})(typeof window !== "undefined" ? window : this);
