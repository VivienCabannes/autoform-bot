/* Review surface client — mission-control dashboard (N-tier).
 *
 * Home screen (any tier present — clusters / statements / declarations):
 *   - renders the verdict-recolored DOT with d3-graphviz (CDN, lazy); the default
 *     home is the lowest tier present, fully collapsed;
 *   - CLICK-TO-UNROLL at ANY tier: clicking a node that HAS CHILDREN at the current
 *     tier fetches /api/dot?tier=<cur>&expand=… and re-renders into a FRESH element
 *     (no full reload); clicking an expanded box (or its bar chip) collapses it;
 *     clicking a LEAF (no children one tier down) routes to /node/<id>;
 *   - EXPANDED-NODES BAR: a reliable HTML strip above the graph, one chip per
 *     expanded node, each with "collapse" and "open in tier N+1 ▸" (→
 *     /?tier=<cur+1>&focus=<id>) — never injected into the SVG;
 *   - FOCUS MODE: when __RV_FOCUS__ is set (?focus=<parent> one tier up), after each
 *     render the member nodes get a steady focus ring (distinct from the agent
 *     pulse), non-members are de-emphasized, and a "Showing … in context · ‹ back"
 *     banner is shown;
 *   - decorates the SVG (45° hatch overlay for tainted nodes; dashed-ring / solid
 *     encodings arrive baked into the DOT);
 *   - ACTIVITY PANEL: polls /api/agents every ~2.5s, renders the orchestrator pill
 *     + a card per active agent, and pulses the target node(s) in the graph
 *     (pulsing the collapsed parent when the target is hidden inside it) — at any
 *     tier, via the tier-agnostic topology;
 *   - keeps an offline fallback (node/parent list; expanding reveals children).
 *
 * Local views for large graphs (PART 5-7):
 *   - TOO-LARGE PLACEHOLDER: when __RV_TOO_LARGE__ is set (a flat tier with more
 *     nodes than the render cap), the d3 render is skipped entirely; the graph area
 *     shows a friendly card + a SEARCH box filtering __RV_NODES__ by id into a short
 *     clickable list (pick → /?tier=N&anchor=<id>, that node's bounded neighborhood);
 *   - NEIGHBORHOOD / ANCHOR view: a focus/anchor subgraph (bounded) renders normally;
 *     an anchor view shows a banner with "expand ±1 hop" (radius+1, clamped 3) and
 *     "‹ back" — child-click → /node/<id> and parent unroll still work;
 *   - the node packet gains a "view neighborhood ▸" link → /?tier=<node tier>&anchor
 *     =<id> (the tier comes from window.__RV_NODE_TIER__).
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

  // Human-facing tier labels (mirrors the server's TIER_LABELS) for the bar/banner.
  var TIER_LABELS = { 1: "clusters", 2: "statements", 3: "declarations" };
  function tierLabel(t) { return TIER_LABELS[t] || ("tier " + t); }

  // ---- shared home state (the client owns `expanded`) ----
  var TIER = window.__RV_TIER__ || 2;
  var TIERS = window.__RV_TIERS__ || [TIER];
  var DOT_URL = window.__RV_DOT_URL__ || "/api/dot";
  var AGENTS_URL = window.__RV_AGENTS_URL__ || "/api/agents";
  var FOCUS = window.__RV_FOCUS__ || null;   // {parent,label,members:[ids]} or null

  // ---- local-view globals (PART 5-7) ----
  // A focus/anchor view is a bounded subgraph that renders normally; an anchor view
  // also carries {id,radius} (drives the "±K hops · ‹ back" banner + "expand ±1 hop").
  // A flat too-large tier renders a placeholder + entry-point picker instead of d3.
  var NEIGHBORHOOD = !!window.__RV_NEIGHBORHOOD__;     // focus OR anchor local view
  var ANCHOR = window.__RV_ANCHOR__ || null;           // {id, radius} or null
  var TOO_LARGE = window.__RV_TOO_LARGE__ || null;      // {tier,count,threshold} | null
  var TOO_LARGE_NODES = window.__RV_NODES__ || null;    // [{id,label,parent}] picker
  var NB_MAX_RADIUS = 3;   // mirrors serve_review.NB_MAX_RADIUS (anchor clamp)

  var home = {
    dot: window.__RV_DOT__ || null,
    state: window.__RV_STATE__ || {},
    idBySlug: window.__RV_IDBYSLUG__ || {},
    kinds: window.__RV_KINDS__ || {},
    topo: window.__RV_TOPO__ || { children: {}, parents: {} },
    expanded: new Set(window.__RV_EXPANDED__ || []),
    mount: null,
    bar: null,            // the expanded-nodes bar element (HTML, above the graph)
    graphvizReady: false,
    rendering: false,
    targets: [],          // current agent target node ids (from /api/agents)
    online: true          // graphviz available (vs offline fallback)
  };

  // ---- tier-agnostic topology (replaces the tier-1-only state.clusters logic) ----
  // A node "has children" iff it appears as a key in topo.children; that one tier
  // down is exactly what /api/dot would expand. parentOf maps a tier-(N+1) child to
  // the (collapsed) box it lives in.
  function childMap() { return (home.topo && home.topo.children) || {}; }
  function parentMap() { return (home.topo && home.topo.parents) || {}; }
  function hasChildren(id) {
    return Object.prototype.hasOwnProperty.call(childMap(), id);
  }
  function parentOf(id) { return parentMap()[id]; }
  function nodeLabel(id) {
    // Prefer the focus label for the focused parent; else a roll-up cluster name if
    // present; else the id itself. (Names aren't in the boot, so the id is the
    // dependable fallback — matches the DOT node labels.)
    if (FOCUS && id === FOCUS.parent && FOCUS.label) return FOCUS.label;
    var cl = (home.state && home.state.clusters) || {};
    return id;
  }

  // ---- home screen bootstrap ----
  function initHome() {
    var mount = document.getElementById("rv-graph");
    if (!mount || !home.dot) return;
    home.mount = mount;
    home.bar = document.getElementById("rv-expanded-bar");

    // Local-view banners are static context (server told us which view this is):
    //   focus → "Showing the <tier> of <unit> in context · ‹ back"
    //   anchor → "Neighborhood of <anchor> (±K hops) · ‹ back" + "expand ±1 hop"
    renderFocusBanner();
    renderNeighborhoodBanner();

    // Start the activity panel immediately (independent of graphviz availability).
    initActivity();

    // PART 5 — too-large flat tier: do NOT run d3. Render a placeholder card + an
    // entry-point picker (search → /?tier=N&anchor=<id>) in the graph area. The
    // server already handed us an empty placeholder DOT, so a stray render couldn't
    // draw the N-node graph, but we short-circuit before even loading graphviz.
    if (TOO_LARGE) {
      renderTooLarge(mount);
      return;
    }

    loadGraphviz(function (ok) {
      if (ok && window.d3 && window.d3.select(mount).graphviz) {
        home.online = true;
        renderGraph(home.dot, false);
      } else {
        home.online = false;
        renderFallback(mount);
      }
      renderExpandedBar();
    });
  }

  // ---- PART 5: too-large placeholder + entry-point picker ----------------------
  // When the flat tier is too big to render, show a friendly card with guidance and
  // a SEARCH box that filters __RV_NODES__ by id into a short clickable list; picking
  // a node navigates to /?tier=N&anchor=<id> (its bounded neighborhood). We never run
  // the d3 render in this state.
  function renderTooLarge(mount) {
    var tier = TOO_LARGE.tier;
    var count = TOO_LARGE.count;
    var nodes = TOO_LARGE_NODES || [];
    var label = tierLabel(tier);

    var card = document.createElement("div");
    card.className = "rv-toolarge";
    card.innerHTML =
      "<div class='rv-tl-head'>"
      + "<span class='rv-tl-ico'>▦</span>"
      + "<h3 class='rv-tl-title'>This graph has " + count + " " + escapeHtml(label)
      + " — too many to show at once.</h3>"
      + "</div>"
      + "<p class='rv-tl-guide'>Rendering " + count + " nodes at once hangs the "
      + "browser. Open a cluster → unit to drill in, or pick a node below to view its "
      + "<strong>neighborhood</strong> (the node plus what it depends on / what "
      + "depends on it, bounded). <em class='rv-tl-shift'>Shift-click an item to open "
      + "its details instead.</em></p>"
      + "<div class='rv-tl-search'>"
      + "<input type='search' id='rv-tl-input' class='rv-tl-input' "
      + "placeholder='search " + count + " " + escapeHtml(label)
      + " by id…' autocomplete='off' spellcheck='false'>"
      + "<span class='rv-tl-hint' id='rv-tl-hint'></span>"
      + "</div>"
      + "<ul class='rv-tl-list' id='rv-tl-list'></ul>";
    mount.innerHTML = "";
    mount.appendChild(card);

    var input = card.querySelector("#rv-tl-input");
    var list = card.querySelector("#rv-tl-list");
    var hint = card.querySelector("#rv-tl-hint");
    var MAX_SHOWN = 25;   // a SHORT list; the search narrows it

    function jumpTo(id) {
      window.location.href = "/?tier=" + encodeURIComponent(tier)
        + "&anchor=" + encodeURIComponent(id);
    }

    function renderList(q) {
      var needle = (q || "").trim().toLowerCase();
      var matches = nodes.filter(function (n) {
        if (!needle) return true;
        var id = String(n.id || "").toLowerCase();
        var lbl = String(n.label || "").toLowerCase();
        return id.indexOf(needle) !== -1 || lbl.indexOf(needle) !== -1;
      });
      var shown = matches.slice(0, MAX_SHOWN);
      if (!matches.length) {
        list.innerHTML = "<li class='rv-tl-none'>no " + escapeHtml(label)
          + " match “" + escapeHtml(needle) + "”</li>";
        hint.textContent = "0 of " + count;
        return;
      }
      hint.textContent = (matches.length > shown.length
        ? "showing " + shown.length + " of " + matches.length + " matches"
        : matches.length + (needle ? " match" + (matches.length === 1 ? "" : "es")
                                   : " " + label));
      var html = "";
      shown.forEach(function (n) {
        var id = n.id;
        var sub = (n.label && n.label !== id)
          ? "<span class='rv-tl-sub'>" + escapeHtml(n.label) + "</span>" : "";
        var par = n.parent
          ? "<span class='rv-tl-parent'>in " + escapeHtml(n.parent) + "</span>" : "";
        html += "<li class='rv-tl-item' data-id='" + escapeHtml(id) + "'>"
          + "<button type='button' class='rv-tl-pick' data-id='" + escapeHtml(id)
          + "'><span class='rv-tl-id'>" + escapeHtml(id) + "</span>"
          + sub + par + "<span class='rv-tl-go'>view neighborhood ▸</span>"
          + "</button></li>";
      });
      list.innerHTML = html;
      list.querySelectorAll(".rv-tl-pick").forEach(function (btn) {
        btn.addEventListener("click", function (ev) {
          var id = btn.getAttribute("data-id");
          // SHIFT-click → that node's detail packet; plain click → its neighborhood.
          if (ev.shiftKey) { openDetails(id); return; }
          jumpTo(id);
        });
      });
    }

    renderList("");
    input.addEventListener("input", function () { renderList(input.value); });
    // Enter on a sole match jumps straight in.
    input.addEventListener("keydown", function (ev) {
      if (ev.key !== "Enter") return;
      var needle = input.value.trim().toLowerCase();
      var matches = nodes.filter(function (n) {
        var id = String(n.id || "").toLowerCase();
        var lbl = String(n.label || "").toLowerCase();
        return id.indexOf(needle) !== -1 || lbl.indexOf(needle) !== -1;
      });
      if (matches.length === 1) jumpTo(matches[0].id);
    });
    input.focus();
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
      applyFocusRing();
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

  // Fetch a fresh DOT for the current tier + `expanded` set and re-render with a
  // transition. Uses the CURRENT tier (not a hardcoded tier-1), so the unroll works
  // identically at tier 1 → 2 and tier 2 → 3.
  function refetchAndRender() {
    if (!home.online) { renderFallback(home.mount); renderExpandedBar(); return; }
    var expand = Array.from(home.expanded).join(",");
    var url = DOT_URL + "?tier=" + encodeURIComponent(TIER)
      + "&expand=" + encodeURIComponent(expand);
    fetch(url).then(function (r) { return r.json(); }).then(function (res) {
      home.dot = res.dot;
      if (res.id_by_slug) home.idBySlug = res.id_by_slug;
      if (res.kinds) home.kinds = res.kinds;
      if (res.topo) home.topo = res.topo;
      renderGraph(res.dot, true);
      renderExpandedBar();
    }).catch(function () {
      // Network hiccup: leave the current graph; the panel still polls.
    });
  }

  function expandNode(id) {
    if (home.rendering || home.expanded.has(id) || !hasChildren(id)) return;
    home.expanded.add(id);
    refetchAndRender();
  }
  function collapseNode(id) {
    if (home.rendering || !home.expanded.has(id)) return;
    home.expanded.delete(id);
    refetchAndRender();
  }

  // ---- expanded-nodes bar (reliable HTML above the graph) ----------------------
  // One chip per expanded node, each carrying "collapse" + "open in tier N+1 ▸".
  // This is the deep-drill control: rather than injecting buttons into the fragile
  // SVG, we render plain HTML the SVG can't break, keyed off home.expanded.
  function renderExpandedBar() {
    var bar = home.bar || (home.bar = document.getElementById("rv-expanded-bar"));
    if (!bar) return;
    var ids = Array.from(home.expanded).filter(hasChildren).sort();
    if (!ids.length) { bar.innerHTML = ""; bar.style.display = "none"; return; }
    bar.style.display = "";

    var deeper = TIER + 1;
    var canDrill = TIERS.indexOf(deeper) !== -1;   // is tier N+1 actually present?
    var html = "<span class='rv-xb-label'>expanded</span>"
      + "<ul class='rv-xb-chips'>";
    ids.forEach(function (id) {
      var label = nodeLabel(id);
      html += "<li class='rv-xb-chip' data-id='" + escapeHtml(id) + "'>"
        + "<span class='rv-xb-name' title='" + escapeHtml(label) + "'>"
        + escapeHtml(label) + "</span>"
        + "<button type='button' class='rv-xb-collapse' "
        + "data-id='" + escapeHtml(id) + "' "
        + "title='collapse this node'>collapse</button>";
      if (canDrill) {
        html += "<a class='rv-xb-open' href='/?tier=" + encodeURIComponent(deeper)
          + "&focus=" + encodeURIComponent(id) + "' "
          + "title='open " + escapeHtml(label) + " in the tier-" + deeper + " ("
          + escapeHtml(tierLabel(deeper)) + ") graph, focused'>"
          + "open in " + deeper + " · " + escapeHtml(tierLabel(deeper)) + " ▸</a>";
      }
      html += "</li>";
    });
    html += "</ul>";
    bar.innerHTML = html;

    bar.querySelectorAll(".rv-xb-collapse").forEach(function (btn) {
      btn.addEventListener("click", function (ev) {
        ev.preventDefault();
        collapseNode(btn.getAttribute("data-id"));
      });
    });
    // Hovering a chip highlights its box in the graph (HTML ⇄ SVG cross-link).
    bar.querySelectorAll(".rv-xb-chip").forEach(function (chip) {
      var id = chip.getAttribute("data-id");
      chip.addEventListener("mouseenter", function () { hoverBox(id, true); });
      chip.addEventListener("mouseleave", function () { hoverBox(id, false); });
    });
  }

  function hoverBox(id, on) {
    var mount = home.mount;
    if (!mount) return;
    var svg = mount.querySelector("svg");
    if (!svg) return;
    var box = svg.querySelector("g.cluster[data-rv-cluster='" + cssEsc(id) + "']");
    if (box) box.classList.toggle("rv-box-hover", !!on);
  }

  // ---- focus mode: banner + steady ring on member nodes ------------------------
  function renderFocusBanner() {
    var slot = document.getElementById("rv-focus-banner");
    if (!slot) return;
    if (!FOCUS || !FOCUS.parent) { slot.innerHTML = ""; slot.style.display = "none"; return; }
    slot.style.display = "";
    var label = FOCUS.label || FOCUS.parent;
    var n = (FOCUS.members || []).length;
    // "‹ back" returns to the parent's own tier (one up), un-focused.
    var backTier = TIER - 1;
    var back = TIERS.indexOf(backTier) !== -1
      ? "/?tier=" + encodeURIComponent(backTier)
      : "/?tier=" + encodeURIComponent(TIER);
    slot.innerHTML =
      "<span class='rv-fb-ico'>◎</span>"
      + "<span class='rv-fb-text'>Showing the " + escapeHtml(tierLabel(TIER))
      + " of <strong>" + escapeHtml(label) + "</strong> in context"
      + (n ? " <span class='rv-fb-count'>(" + n + ")</span>" : "")
      + " <span class='rv-fb-nb'>+ immediate neighbors</span>"
      + "</span>"
      + "<a class='rv-fb-back' href='" + back + "'>‹ back</a>";
  }

  // ---- PART 6: anchor-view banner ("Neighborhood of ‹anchor› (±K hops)") --------
  // For an anchor view (?anchor=<id>&radius=K) we render a banner — into the same
  // slot the focus banner uses (focus/anchor are mutually exclusive on the server) —
  // carrying the anchor label and two controls:
  //   "expand ±1 hop" → re-navigate with radius+1 (clamped to NB_MAX_RADIUS=3);
  //   "‹ back"        → leave the local view (back to the flat tier this came from).
  // Child clicks → /node/<id> and parent unroll still work (the subgraph renders
  // normally), so this banner is the only anchor-specific chrome.
  function renderNeighborhoodBanner() {
    if (!ANCHOR || FOCUS) return;   // focus uses renderFocusBanner; flat: nothing
    var slot = document.getElementById("rv-focus-banner");
    if (!slot) return;
    slot.style.display = "";
    slot.classList.add("rv-anchor-banner");
    var id = ANCHOR.id;
    var k = Number(ANCHOR.radius) || 1;
    // "‹ back" drops the anchor → the flat tier-N view (no anchor/radius).
    var back = "/?tier=" + encodeURIComponent(TIER);
    var canExpand = k < NB_MAX_RADIUS;
    var nextK = Math.min(k + 1, NB_MAX_RADIUS);
    var expandHref = "/?tier=" + encodeURIComponent(TIER)
      + "&anchor=" + encodeURIComponent(id) + "&radius=" + nextK;

    var html =
      "<span class='rv-fb-ico'>⊚</span>"
      + "<span class='rv-fb-text'>Neighborhood of <strong>" + escapeHtml(id)
      + "</strong> <span class='rv-fb-count'>(±" + k + " hop"
      + (k === 1 ? "" : "s") + ")</span></span>"
      + "<span class='rv-nb-controls'>";
    if (canExpand) {
      html += "<a class='rv-nb-expand' href='" + expandHref + "' "
        + "title='widen to ±" + nextK + " hops (more neighbors, still bounded)'>"
        + "expand ±1 hop ▸</a>";
    } else {
      html += "<span class='rv-nb-expand rv-nb-maxed' "
        + "title='already at the maximum radius (±" + NB_MAX_RADIUS + ")'>"
        + "max radius (±" + NB_MAX_RADIUS + ")</span>";
    }
    html += "</span>"
      + "<a class='rv-fb-back' href='" + back + "'>‹ back</a>";
    slot.innerHTML = html;
  }

  // After each render: ring the focus members + de-emphasize non-members. Distinct
  // from the agent pulse (a steady amber-accent ring vs the pulsing accent glow).
  function applyFocusRing() {
    var mount = home.mount;
    if (!mount) return;
    var svg = mount.querySelector("svg");
    if (!svg || !FOCUS || !(FOCUS.members && FOCUS.members.length)) return;
    var members = {};
    FOCUS.members.forEach(function (id) { members[id] = true; });
    svg.classList.add("rv-has-focus");
    svg.querySelectorAll("g.node[data-rv-id]").forEach(function (g) {
      var id = g.getAttribute("data-rv-id");
      if (members[id]) {
        g.classList.add("rv-focus-member");
        g.classList.remove("rv-focus-dim");
      } else {
        g.classList.add("rv-focus-dim");
      }
    });
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

    // 1) Plain nodes at the current tier: a PARENT (has children one tier down) →
    //    unroll in place; a LEAF → route to its packet. Works at any tier because
    //    "has children?" comes from the tier-agnostic topology, not state.clusters.
    svg.querySelectorAll("g.node").forEach(function (g) {
      var titleEl = g.querySelector("title");
      var slug = titleEl ? titleEl.textContent.trim() : "";
      var id = home.idBySlug[slug] || slug;
      if (!id) return;
      g.setAttribute("data-rv-id", id);
      g.style.cursor = "pointer";

      if (hasChildren(id) && !home.expanded.has(id)) {
        // Collapsed parent → click unrolls it; SHIFT-click opens its detail packet.
        g.classList.add("rv-clusternode");
        g.addEventListener("click", function (ev) {
          ev.stopPropagation();
          if (ev.shiftKey) { openDetails(id); return; }
          expandNode(id);
        });
      } else {
        // Leaf (or a child now drawn inside an open box) → click opens its packet
        // (shift-click does the same — there is nothing to unroll).
        g.addEventListener("click", function (ev) {
          ev.stopPropagation();
          openDetails(id);
        });
        if (tainted[id] && colors[id] !== "in_mathlib") {
          overlayHatch(g);
        }
      }
    });

    // 2) Expanded boxes: clicking the box (its label/background) collapses it. The
    //    box is titled "cluster_<slug>"; map the slug back to the node id.
    svg.querySelectorAll("g.cluster").forEach(function (g) {
      var titleEl = g.querySelector("title");
      var t = titleEl ? titleEl.textContent.trim() : "";
      var slug = t.indexOf("cluster_") === 0 ? t.slice("cluster_".length) : t;
      var id = home.idBySlug[slug] || slug;
      if (!id || !home.expanded.has(id)) return;
      g.setAttribute("data-rv-cluster", id);
      g.classList.add("rv-clusterbox");
      var label = g.querySelector("text");
      if (label) label.style.cursor = "pointer";
      g.style.cursor = "pointer";
      g.addEventListener("click", function (ev) {
        // Only collapse when the box chrome itself is clicked — a click that
        // bubbled up from a child node already routed (and stopped) above.
        // SHIFT-click opens the parent's own detail packet instead of collapsing.
        ev.stopPropagation();
        if (ev.shiftKey) { openDetails(id); return; }
        collapseNode(id);
      });
    });
  }

  // Open a node's detail packet (/node/<id>). Reached by clicking a leaf, or by
  // SHIFT-clicking any node (so you can inspect a parent's specifics without
  // unrolling it — see decorate()).
  function openDetails(id) {
    if (!id) return;
    window.location.href = "/node/" + encodeURIComponent(id);
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
  // parent, pulse that parent node instead. Walks parents UP the topology so a
  // deeply-nested target still surfaces. Re-applied after each (re)render and
  // whenever the poll changes the target set. Tier-agnostic.
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
    var boxes = {}; // node id -> g.cluster element (open box)
    svg.querySelectorAll("g.cluster[data-rv-cluster]").forEach(function (g) {
      boxes[g.getAttribute("data-rv-cluster")] = g;
    });

    home.targets.forEach(function (tid) {
      if (visible[tid]) {                 // target node itself is on screen
        visible[tid].classList.add("rv-pulse");
        return;
      }
      // Walk up the parent chain to the nearest collapsed ancestor that is visible.
      var p = parentOf(tid);
      while (p) {
        if (visible[p]) { visible[p].classList.add("rv-pulse"); return; }
        if (boxes[p]) { boxes[p].classList.add("rv-pulse"); return; }
        p = parentOf(p);
      }
    });
  }

  // ---- offline fallback: node/parent list; expanding reveals children ----
  function renderFallback(mount) {
    if (!mount) return;
    var state = home.state || {};
    var cmap = childMap();
    // A parent at the current tier is a node that (a) has children one tier down and
    // (b) is itself drawn at this tier — i.e. its own parent isn't at this tier. We
    // approximate "drawn at this tier" by: not a child of another current-tier node.
    var pmap = parentMap();
    var ids = Object.keys(state.verdicts || {});
    // The roots to list = current-tier nodes. We don't carry tiers per id client-
    // side, so use the DOT's own node set: parse titles isn't reliable pre-render,
    // so fall back to listing every parent that has children + every leaf, deduped
    // by "is this id a child of a node we're also listing". Simplest robust choice:
    // list the same set the server drew — read it from colors keys filtered to the
    // tier via topo (a node at TIER has no parent at TIER, and its children are at
    // TIER+1). Practically: roots = nodes whose parent is NOT in the graph's current
    // tier set. Since we can't know tiers exactly offline, list parents+leaves that
    // are not themselves listed as someone's child within this set.
    var hasChildList = Object.keys(cmap);
    if (hasChildList.length && TIER < (TIERS[TIERS.length - 1] || TIER)) {
      return renderFallbackParents(mount, state, cmap, pmap);
    }
    return renderFallbackFlat(mount, state);
  }

  // Parent/child fallback (works at any tier with a deeper tier): show the nodes that
  // have children as expandable rows, plus current-tier leaves, colored by trust.
  function renderFallbackParents(mount, state, cmap, pmap) {
    var colors = state.colors || {};
    var verdicts = state.verdicts || {};
    // Current-tier roots: a node is a root of THIS view if it isn't the child of
    // another node in this same view. We list every parent-with-children whose own
    // parent is not also a parent-with-children at this tier (i.e. one tier up).
    var parentIds = Object.keys(cmap).filter(function (pid) {
      var up = pmap[pid];
      return !(up && Object.prototype.hasOwnProperty.call(cmap, up));
    }).sort();

    var html = "<p class='rv-note'>Interactive graph unavailable (offline) — "
      + "static list; expand a node to reveal its children.</p>"
      + "<ul class='rv-fallback rv-fallback-clusters'>";
    parentIds.forEach(function (pid) {
      var cs = colors[pid] || verdicts[pid] || "unreviewed";
      var open = home.expanded.has(pid);
      html += "<li class='rv-fb-cluster'>"
        + "<button type='button' class='rv-fb-toggle rv-" + cs + "' "
        + "data-cid='" + escapeHtml(pid) + "'>"
        + (open ? "▾ " : "▸ ") + escapeHtml(pid)
        + " <span class='rv-fb-v'>" + escapeHtml(cs) + "</span></button>";
      if (open) {
        html += "<ul class='rv-fallback rv-fb-children'>";
        (cmap[pid] || []).forEach(function (ch) {
          var ccs = colors[ch] || verdicts[ch] || "unreviewed";
          var leaf = !Object.prototype.hasOwnProperty.call(cmap, ch);
          html += "<li class='rv-fb rv-" + ccs + "'>"
            + (leaf
              ? "<a href='/node/" + encodeURIComponent(ch) + "'>" + escapeHtml(ch) + "</a>"
              : "<span class='rv-fb-haschild'>" + escapeHtml(ch) + " ▸</span>")
            + " <span class='rv-fb-v'>" + escapeHtml(ccs) + "</span></li>";
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
        renderFallbackParents(mount, state, cmap, pmap);
        renderExpandedBar();
      });
    });
  }

  // Flat fallback (deepest tier, or a graph with no nesting): one chip per node.
  function renderFallbackFlat(mount, state) {
    var verdicts = state.verdicts || {};
    var colors = state.colors || {};
    var sources = state.sources || {};
    var tainted = {};
    (state.tainted || []).forEach(function (id) { tainted[id] = true; });
    var members = {};
    if (FOCUS && FOCUS.members) FOCUS.members.forEach(function (id) { members[id] = true; });

    // At the deepest tier, only show that tier's nodes: leaves (no children). A node
    // with children belongs to a shallower tier, so skip it here.
    var cmap = childMap();
    var ids = Object.keys(verdicts).filter(function (id) {
      return !Object.prototype.hasOwnProperty.call(cmap, id);
    }).sort();

    var html = "<p class='rv-note'>Interactive graph unavailable (offline) — "
      + "static node list, colored by trust state.</p><ul class='rv-fallback'>";
    ids.forEach(function (id) {
      var v = verdicts[id] || "unreviewed";
      var cs = colors[id] || v;
      var blue = cs === "in_mathlib";
      var isTainted = tainted[id] && !blue;
      var src = sources[id] === "human" ? "human" : "ai";
      var label = blue ? "in Mathlib" : v;
      var foc = FOCUS && FOCUS.members ? (members[id] ? " rv-focus-member" : " rv-focus-dim") : "";
      html += "<li class='rv-fb rv-" + cs + (isTainted ? " rv-tainted" : "") + foc
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

  // ---- PART 7: node packet "view neighborhood ▸" link --------------------------
  // Re-center the bounded local view from any node: /?tier=<this node's tier>&anchor
  // =<id>. The node's tier comes from the server (window.__RV_NODE_TIER__, the same
  // node_tier rule used everywhere); we only offer it when that tier can actually be
  // anchored (it's a real tier present in the graph). Inserted at the top of the
  // packet's right column, next to the node id.
  function initNeighborhoodLink() {
    var nodeId = window.__RV_NODE__;
    if (!nodeId) return;                       // not the node screen
    var right = document.querySelector(".rv-packet-right");
    if (!right) return;
    var tier = window.__RV_NODE_TIER__;
    var tiers = window.__RV_TIERS__ || [];
    if (tier == null || tiers.indexOf(tier) === -1) return;   // nowhere to anchor

    var href = "/?tier=" + encodeURIComponent(tier)
      + "&anchor=" + encodeURIComponent(nodeId);
    var wrap = document.createElement("div");
    wrap.className = "rv-nb-link-row";
    wrap.innerHTML =
      "<a class='rv-nb-link' href='" + href + "' "
      + "title='view this node’s bounded neighborhood in the "
      + escapeHtml(tierLabel(tier)) + " graph'>"
      + "view neighborhood ▸</a>"
      + "<span class='rv-nb-link-sub'>this " + escapeHtml(tierLabel(tier))
      + " node + its deps / dependents (bounded)</span>";
    right.insertBefore(wrap, right.firstChild);
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;",
        "'": "&#39;" }[c];
    });
  }

  // Escape a value for use inside a CSS attribute selector (data-rv-cluster='…').
  function cssEsc(s) {
    return String(s).replace(/['"\\]/g, "\\$&");
  }

  document.addEventListener("DOMContentLoaded", function () {
    initHome();
    initVerdictForm();
    initNeighborhoodLink();
  });
})();
