/* Review surface client — mission-control dashboard.
 *
 * Home screen:
 *   - renders the verdict-recolored DOT with d3-graphviz (CDN, lazy) — tier-1 by
 *     default, clusters collapsed;
 *   - CLICK-TO-UNROLL: clicking a collapsed cluster fetches /api/dot with the new
 *     `expand` set and re-renders WITH A TRANSITION (no full reload); clicking an
 *     expanded cluster's label collapses it; clicking a tier-2 child routes to
 *     /node/<id>;
 *   - decorates the SVG (45° hatch overlay for tainted nodes; dashed-ring / solid
 *     encodings arrive baked into the DOT);
 *   - ACTIVITY PANEL: polls /api/agents every ~2.5s, renders the orchestrator pill
 *     + a card per active agent, and pulses the target node(s) in the graph
 *     (pulsing the collapsed cluster when the target is hidden inside it);
 *   - keeps an offline fallback (cluster list; expanding reveals children).
 *
 * Node screen: POST the human verdict to /api/verdict/<id> and reflect the delta.
 *
 * No build step, no framework — plain DOM. d3/d3-graphviz are loaded lazily from a
 * CDN only on the home screen; offline, the static fallback keeps the page usable.
 */
(function () {
  "use strict";

  var PALETTE = window.__RV_PALETTE__ || {
    in_mathlib: "#2563B0", clean: "#2F7D4F", flagged: "#C08A1E",
    rejected: "#C0392B", grey: "#C9C2B4", accent: "#1A4B8C", ink: "#1F1D1A"
  };

  var POLL_MS = 2500;          // /api/agents poll cadence (~2.5s, per spec)
  var TRANSITION_MS = 450;     // d3-graphviz expand/collapse transition

  // ---- shared home state (the client owns `expanded`) ----
  var TIER = window.__RV_TIER__ || 2;
  var DOT_URL = window.__RV_DOT_URL__ || "/api/dot";
  var AGENTS_URL = window.__RV_AGENTS_URL__ || "/api/agents";

  var home = {
    dot: window.__RV_DOT__ || null,
    state: window.__RV_STATE__ || {},
    idBySlug: window.__RV_IDBYSLUG__ || {},
    kinds: window.__RV_KINDS__ || {},
    expanded: new Set(window.__RV_EXPANDED__ || []),
    mount: null,
    graphvizReady: false,
    rendering: false,
    targets: [],          // current agent target node ids (from /api/agents)
    online: true          // graphviz available (vs offline fallback)
  };

  // The set of tier-1 cluster ids (from /api/state.clusters): lets us tell a
  // collapsed-cluster node apart from a tier-2 statement node in the SVG.
  function clusterIds() {
    var c = (home.state && home.state.clusters) || {};
    return c;
  }
  function isCluster(id) { return Object.prototype.hasOwnProperty.call(clusterIds(), id); }

  // parent(child id) -> cluster id, from state.clusters[*].children.
  var _parentOf = null;
  function parentOf(id) {
    if (!_parentOf) {
      _parentOf = {};
      var cl = clusterIds();
      Object.keys(cl).forEach(function (cid) {
        (cl[cid].children || []).forEach(function (ch) { _parentOf[ch] = cid; });
      });
    }
    return _parentOf[id];
  }

  // ---- home screen bootstrap ----
  function initHome() {
    var mount = document.getElementById("rv-graph");
    if (!mount || !home.dot) return;
    home.mount = mount;

    // Start the activity panel immediately (independent of graphviz availability).
    initActivity();

    loadGraphviz(function (ok) {
      if (ok && window.d3 && window.d3.select(mount).graphviz) {
        home.online = true;
        renderGraph(home.dot, false);
      } else {
        home.online = false;
        renderFallback(mount);
      }
    });
  }

  // Render `dot` into the mount via d3-graphviz. `transition` => animate from the
  // current layout to the new one (used on expand/collapse). On first paint we skip
  // the transition (nothing to morph from) but still fit.
  function renderGraph(dot, transition) {
    var mount = home.mount;
    if (!mount) return;
    home.rendering = true;
    var loading = mount.querySelector(".rv-graph-loading");
    if (loading) loading.remove();
    // Render into a FRESH child element each time. d3-graphviz throws when it
    // re-renders a structurally different graph on a *reused* instance — an unroll
    // introduces a `subgraph cluster_…`, which is exactly that case. A brand-new
    // element carries no stale renderer state, so every (re)render is a clean
    // first-render. Keep the old graph visible until the new one settles, then swap.
    var stage = document.createElement("div");
    stage.className = "rv-graph-stage";
    stage.style.cssText = "width:100%;height:100%;";
    mount.appendChild(stage);
    var settled = false;
    function settle() {
      if (settled) return;
      settled = true;
      home.rendering = false;
      // Drop every prior stage, keeping only the one we just rendered.
      var stages = mount.querySelectorAll(".rv-graph-stage");
      for (var i = 0; i < stages.length; i++) {
        if (stages[i] !== stage) stages[i].remove();
      }
      decorate(mount);
      applyPulse();
    }
    try {
      var gv = window.d3.select(stage).graphviz({ useWorker: false }).fit(true);
      gv.renderDot(dot);
      gv.on("end", settle);
      // Safety: settle even if d3-graphviz's "end" event doesn't fire in this build.
      setTimeout(settle, 700);
    } catch (e) {
      settled = true;
      home.rendering = false;
      stage.remove();
      home.online = false;
      renderFallback(mount);
    }
  }

  // Fetch a fresh DOT for the current `expanded` set and re-render with a transition.
  function refetchAndRender() {
    if (!home.online) { renderFallback(home.mount); return; }
    var expand = Array.from(home.expanded).join(",");
    var url = DOT_URL + "?tier=1&expand=" + encodeURIComponent(expand);
    fetch(url).then(function (r) { return r.json(); }).then(function (res) {
      home.dot = res.dot;
      if (res.id_by_slug) home.idBySlug = res.id_by_slug;
      if (res.kinds) home.kinds = res.kinds;
      renderGraph(res.dot, true);
    }).catch(function () {
      // Network hiccup: leave the current graph; the panel still polls.
    });
  }

  function expandCluster(id) {
    if (home.rendering || home.expanded.has(id)) return;
    home.expanded.add(id);
    refetchAndRender();
  }
  function collapseCluster(id) {
    if (home.rendering || !home.expanded.has(id)) return;
    home.expanded.delete(id);
    refetchAndRender();
  }

  // ---- SVG decoration: wire clicks (unroll / collapse / route) + hatch taint ----
  function decorate(mount) {
    var svg = mount.querySelector("svg");
    if (!svg) return;
    ensureHatchPattern(svg);
    ensurePulseFilter(svg);

    var tainted = {};
    (home.state.tainted || []).forEach(function (id) { tainted[id] = true; });
    var colors = home.state.colors || {};

    // 1) Plain nodes: tier-1 collapsed clusters (id is a cluster) OR tier-2 children
    //    (inside an expanded subgraph, or the whole graph in tier-2 mode).
    svg.querySelectorAll("g.node").forEach(function (g) {
      var titleEl = g.querySelector("title");
      var slug = titleEl ? titleEl.textContent.trim() : "";
      var id = home.idBySlug[slug] || slug;
      if (!id) return;
      g.setAttribute("data-rv-id", id);
      g.style.cursor = "pointer";

      if (TIER === 1 && isCluster(id)) {
        // Collapsed cluster node -> unroll in place.
        g.classList.add("rv-clusternode");
        g.addEventListener("click", function (ev) {
          ev.stopPropagation();
          expandCluster(id);
        });
      } else {
        // Tier-2 statement -> route to its packet.
        g.addEventListener("click", function (ev) {
          ev.stopPropagation();
          window.location.href = "/node/" + encodeURIComponent(id);
        });
        if (tainted[id] && colors[id] !== "in_mathlib") {
          overlayHatch(g);
        }
      }
    });

    // 2) Expanded cluster boxes: clicking the label/box collapses the cluster.
    svg.querySelectorAll("g.cluster").forEach(function (g) {
      var titleEl = g.querySelector("title");
      var t = titleEl ? titleEl.textContent.trim() : "";
      // graphviz titles a subgraph "cluster_<slug>"; map the slug back to the id.
      var slug = t.indexOf("cluster_") === 0 ? t.slice("cluster_".length) : t;
      var id = home.idBySlug[slug] || slug;
      if (!id || !isCluster(id)) return;
      g.setAttribute("data-rv-cluster", id);
      g.classList.add("rv-clusterbox");
      // Make the label feel clickable; clicking anywhere on the box collapses.
      var label = g.querySelector("text");
      if (label) label.style.cursor = "pointer";
      g.style.cursor = "pointer";
      g.addEventListener("click", function (ev) {
        ev.stopPropagation();
        collapseCluster(id);
      });
    });
  }

  function overlayHatch(g) {
    var shape = g.querySelector("ellipse, polygon, path");
    if (!shape) return;
    var clone = shape.cloneNode(true);
    clone.setAttribute("fill", "url(#rv-hatch)");
    clone.setAttribute("fill-opacity", "0.45");
    clone.setAttribute("stroke", "none");
    clone.classList.add("rv-hatch-overlay");
    shape.parentNode.appendChild(clone);
  }

  function ensureHatchPattern(svg) {
    if (svg.querySelector("#rv-hatch")) return;
    var ns = "http://www.w3.org/2000/svg";
    var defs = svg.querySelector("defs") || svg.insertBefore(
      document.createElementNS(ns, "defs"), svg.firstChild);
    var pat = document.createElementNS(ns, "pattern");
    pat.setAttribute("id", "rv-hatch");
    pat.setAttribute("patternUnits", "userSpaceOnUse");
    pat.setAttribute("width", "6");
    pat.setAttribute("height", "6");
    pat.setAttribute("patternTransform", "rotate(45)");
    var line = document.createElementNS(ns, "line");
    line.setAttribute("x1", "0"); line.setAttribute("y1", "0");
    line.setAttribute("x2", "0"); line.setAttribute("y2", "6");
    line.setAttribute("stroke", PALETTE.ink || "#1F1D1A");
    line.setAttribute("stroke-width", "1.4");
    pat.appendChild(line);
    defs.appendChild(pat);
  }

  // A soft accent glow used by the pulsing target ring.
  function ensurePulseFilter(svg) {
    if (svg.querySelector("#rv-glow")) return;
    var ns = "http://www.w3.org/2000/svg";
    var defs = svg.querySelector("defs") || svg.insertBefore(
      document.createElementNS(ns, "defs"), svg.firstChild);
    var f = document.createElementNS(ns, "filter");
    f.setAttribute("id", "rv-glow");
    f.setAttribute("x", "-40%"); f.setAttribute("y", "-40%");
    f.setAttribute("width", "180%"); f.setAttribute("height", "180%");
    var blur = document.createElementNS(ns, "feGaussianBlur");
    blur.setAttribute("stdDeviation", "3");
    blur.setAttribute("result", "b");
    var merge = document.createElementNS(ns, "feMerge");
    ["b", "SourceGraphic"].forEach(function (n) {
      var m = document.createElementNS(ns, "feMergeNode");
      m.setAttribute("in", n);
      merge.appendChild(m);
    });
    f.appendChild(blur); f.appendChild(merge);
    defs.appendChild(f);
  }

  // ---- target pulse: highlight the node(s) agents are working on ----
  // For each target id, pulse its node if visible; if it sits inside a collapsed
  // cluster, pulse that cluster node instead. Re-applied after each (re)render and
  // whenever the poll changes the target set.
  function applyPulse() {
    var mount = home.mount;
    if (!mount) return;
    var svg = mount.querySelector("svg");
    if (!svg) return;

    // Clear any prior pulse marks.
    svg.querySelectorAll(".rv-pulse").forEach(function (g) {
      g.classList.remove("rv-pulse");
    });

    var visible = {};   // data-rv-id -> g.node element
    svg.querySelectorAll("g.node[data-rv-id]").forEach(function (g) {
      visible[g.getAttribute("data-rv-id")] = g;
    });
    var expandedBoxes = {}; // cluster id -> g.cluster element
    svg.querySelectorAll("g.cluster[data-rv-cluster]").forEach(function (g) {
      expandedBoxes[g.getAttribute("data-rv-cluster")] = g;
    });

    home.targets.forEach(function (tid) {
      if (visible[tid]) {                 // target node itself is on screen
        visible[tid].classList.add("rv-pulse");
        return;
      }
      var p = parentOf(tid);
      if (p && visible[p]) {              // hidden inside a COLLAPSED cluster node
        visible[p].classList.add("rv-pulse");
      } else if (p && expandedBoxes[p]) {
        // Expanded but child not yet matched (rare timing) — pulse the box.
        expandedBoxes[p].classList.add("rv-pulse");
      }
    });
  }

  // ---- offline fallback: cluster list; expanding reveals children ----
  function renderFallback(mount) {
    if (!mount) return;
    var state = home.state || {};
    if (TIER === 1) return renderFallbackTier1(mount, state);
    return renderFallbackTier2(mount, state);
  }

  function renderFallbackTier1(mount, state) {
    var clusters = state.clusters || {};
    var cids = Object.keys(clusters).sort();
    var html = "<p class='rv-note'>Interactive graph unavailable (offline) — "
      + "static cluster list; expand a cluster to reveal its statements.</p>"
      + "<ul class='rv-fallback rv-fallback-clusters'>";
    cids.forEach(function (cid) {
      var roll = clusters[cid] || {};
      var v = roll.verdict || "unreviewed";
      var open = home.expanded.has(cid);
      html += "<li class='rv-fb-cluster'>"
        + "<button type='button' class='rv-fb-toggle rv-" + v + "' "
        + "data-cid='" + escapeHtml(cid) + "'>"
        + (open ? "▾ " : "▸ ") + escapeHtml(cid)
        + " <span class='rv-fb-v'>" + escapeHtml(v) + "</span></button>";
      if (open) {
        html += "<ul class='rv-fallback rv-fb-children'>";
        (roll.children || []).forEach(function (ch) {
          var cv = (roll.child_verdicts || {})[ch] || "unreviewed";
          var cs = (state.colors || {})[ch] || cv;
          html += "<li class='rv-fb rv-" + cs + "'>"
            + "<a href='/node/" + encodeURIComponent(ch) + "'>" + escapeHtml(ch)
            + "</a> <span class='rv-fb-v'>" + escapeHtml(cv) + "</span></li>";
        });
        html += "</ul>";
      }
      html += "</li>";
    });
    html += "</ul>";
    mount.innerHTML = html;
    mount.querySelectorAll(".rv-fb-toggle").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var cid = btn.getAttribute("data-cid");
        if (home.expanded.has(cid)) home.expanded.delete(cid);
        else home.expanded.add(cid);
        renderFallbackTier1(mount, state);
      });
    });
  }

  function renderFallbackTier2(mount, state) {
    var verdicts = state.verdicts || {};
    var colors = state.colors || {};
    var sources = state.sources || {};
    var tainted = {};
    (state.tainted || []).forEach(function (id) { tainted[id] = true; });

    var ids = Object.keys(verdicts).sort();
    var html = "<p class='rv-note'>Interactive graph unavailable (offline) — "
      + "static node list, colored by trust state.</p><ul class='rv-fallback'>";
    ids.forEach(function (id) {
      var v = verdicts[id] || "unreviewed";
      var cs = colors[id] || v;
      var blue = cs === "in_mathlib";
      var isTainted = tainted[id] && !blue;
      var src = sources[id] === "human" ? "human" : "ai";
      var label = blue ? "in Mathlib" : v;
      html += "<li class='rv-fb rv-" + cs + (isTainted ? " rv-tainted" : "")
        + (blue ? " rv-solid"
                : (sources[id] === "human" ? " rv-human" : " rv-aionly")) + "'>"
        + "<a href='/node/" + encodeURIComponent(id) + "'>" + escapeHtml(id)
        + "</a> <span class='rv-fb-v'>" + label + "</span>"
        + (!blue && sources[id] ? " <span class='rv-fb-src'>" + src + "</span>" : "")
        + (isTainted ? " <span class='rv-fb-taint'>tainted</span>" : "")
        + "</li>";
    });
    html += "</ul>";
    mount.innerHTML = html;
  }

  function loadGraphviz(done) {
    if (window.d3 && window.d3.select && window.d3.select("body").graphviz) {
      return done(true);
    }
    var srcs = [
      "https://cdn.jsdelivr.net/npm/d3@7/dist/d3.min.js",
      "https://cdn.jsdelivr.net/npm/@hpcc-js/wasm@2/dist/index.min.js",
      "https://cdn.jsdelivr.net/npm/d3-graphviz@5/build/d3-graphviz.min.js"
    ];
    var i = 0;
    function next() {
      if (i >= srcs.length) return done(true);
      var s = document.createElement("script");
      s.src = srcs[i++];
      s.onload = next;
      s.onerror = function () { done(false); };
      document.head.appendChild(s);
    }
    next();
  }

  // ---- activity panel: poll /api/agents, render orchestrator + agent cards ----
  var ROLE_LABEL = { worker: "worker", reviewer: "reviewer", planner: "planner" };

  function initActivity() {
    var panel = document.getElementById("rv-activity");
    if (!panel) return;
    pollAgents(panel);
    setInterval(function () { pollAgents(panel); }, POLL_MS);
  }

  function pollAgents(panel) {
    fetch(AGENTS_URL, { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (feed) {
        renderActivity(panel, feed || {});
        var agents = (feed && feed.agents) || [];
        var next = agents.map(function (a) { return a && a.target; })
          .filter(Boolean);
        // Only re-apply pulse if the target set actually changed.
        if (next.join("|") !== home.targets.join("|")) {
          home.targets = next;
          applyPulse();
        }
      })
      .catch(function () {
        // Keep the last good panel; show a soft offline note once.
        if (!panel.querySelector(".rv-act-offline")) {
          var hdr = panel.querySelector(".rv-act-orch");
          if (hdr) {
            var n = document.createElement("div");
            n.className = "rv-act-offline rv-note";
            n.textContent = "feed unreachable — retrying…";
            hdr.appendChild(n);
          }
        }
      });
  }

  function renderActivity(panel, feed) {
    var orch = feed.orchestrator || { state: "idle" };
    var agents = (feed.agents || []).filter(function (a) {
      return a && (a.role || a.name || a.target);
    });
    var ostate = String(orch.state || "idle");

    var html = "<div class='rv-act-head'>"
      + "<h3 class='rv-act-title'>activity</h3>"
      + "<span class='rv-pill rv-pill-" + escapeHtml(ostate) + "'>"
      + "<span class='rv-pill-dot'></span>" + escapeHtml(ostate) + "</span>"
      + "</div>";

    html += "<div class='rv-act-orch'>";
    if (orch.phase) {
      html += "<div class='rv-orch-phase'>" + escapeHtml(orch.phase) + "</div>";
    }
    if (orch.detail) {
      html += "<div class='rv-orch-detail'>" + escapeHtml(orch.detail) + "</div>";
    }
    if (!orch.phase && !orch.detail) {
      html += "<div class='rv-orch-detail'>orchestrator " + escapeHtml(ostate)
        + "</div>";
    }
    html += "</div>";

    if (!agents.length) {
      html += "<div class='rv-act-empty'>idle — no agents running</div>";
    } else {
      html += "<ul class='rv-agents'>";
      agents.forEach(function (a) {
        var role = String(a.role || "agent").toLowerCase();
        var roleClass = ROLE_LABEL[role] ? role : "agent";
        var target = a.target || "";
        html += "<li class='rv-agent'>"
          + "<div class='rv-agent-top'>"
          + "<span class='rv-rolebadge rv-role-" + escapeHtml(roleClass) + "'>"
          + escapeHtml(role) + "</span>"
          + "<span class='rv-agent-name'>" + escapeHtml(a.name || "agent")
          + "</span>"
          + "</div>"
          + "<div class='rv-agent-body'>"
          + "<span class='rv-agent-state'>" + escapeHtml(a.state || "active")
          + "</span>";
        if (target) {
          html += " <span class='rv-agent-arrow'>→</span> "
            + "<a class='rv-agent-target' href='/node/"
            + encodeURIComponent(target) + "'>" + escapeHtml(target) + "</a>";
        }
        html += "</div></li>";
      });
      html += "</ul>";
    }

    panel.innerHTML = html;
  }

  // ---- node screen: verdict panel POST ----
  function initVerdictForm() {
    var form = document.getElementById("rv-verdict-form");
    if (!form) return;
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      var node = form.getAttribute("data-node");
      var fd = new FormData(form);
      var verdict = fd.get("verdict");
      var status = document.getElementById("rv-vp-status");
      if (!verdict) {
        if (status) { status.textContent = "pick a verdict first"; }
        return;
      }
      var scoreRaw = fd.get("score");
      var payload = {
        verdict: verdict,
        score: scoreRaw === "" || scoreRaw == null ? null : Number(scoreRaw),
        note: fd.get("note") || ""
      };
      if (status) { status.textContent = "saving…"; }
      fetch("/api/verdict/" + encodeURIComponent(node), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      }).then(function (r) { return r.json(); }).then(function (res) {
        if (res.ok) {
          if (status) {
            status.textContent = "saved — effective: " + res.effective
              + " (" + res.tainted.length + " tainted downstream)";
          }
          var badge = form.querySelector(".rv-eff");
          if (badge) {
            badge.className = "rv-eff rv-" + res.effective;
            badge.textContent = "effective: " + res.effective + " (human)";
          }
        } else if (status) {
          status.textContent = "error: " + (res.error || "save failed");
        }
      }).catch(function () {
        if (status) { status.textContent = "network error"; }
      });
    });
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;",
        "'": "&#39;" }[c];
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    initHome();
    initVerdictForm();
  });
})();
