/* Review surface client.
 *
 * Home screen: render the verdict-recolored DOT with d3-graphviz if available,
 * else fall back to a static node list, then decorate the SVG (45° hatch overlay
 * for tainted nodes; the dashed-ring / solid encodings already arrive baked into
 * the DOT's node styles) and route node clicks to /node/<id>.
 *
 * Node screen: POST the human verdict to /api/verdict/<id> and reflect the delta.
 *
 * No build step, no framework — plain DOM. d3/d3-graphviz are loaded lazily from a
 * CDN only on the home screen; if offline, the static fallback keeps the page
 * usable (it still lists every node, colored by verdict, linking to its packet).
 */
(function () {
  "use strict";

  var PALETTE = window.__RV_PALETTE__ || {
    in_mathlib: "#2563B0", clean: "#2F7D4F", flagged: "#C08A1E",
    rejected: "#C0392B", grey: "#C9C2B4"
  };

  // ---- home screen ----
  function initHome() {
    var dot = window.__RV_DOT__;
    var state = window.__RV_STATE__ || {};
    var idBySlug = window.__RV_IDBYSLUG__ || {};
    var mount = document.getElementById("rv-graph");
    if (!mount || !dot) return;

    loadGraphviz(function (ok) {
      if (ok && window.d3 && window.d3.select(mount).graphviz) {
        renderWithGraphviz(mount, dot, idBySlug, state);
      } else {
        renderFallback(mount, idBySlug, state);
      }
    });
  }

  function renderWithGraphviz(mount, dot, idBySlug, state) {
    try {
      window.d3.select(mount).graphviz({ useWorker: false })
        .fit(true)
        .renderDot(dot)
        .on("end", function () { decorate(mount, idBySlug, state); });
    } catch (e) {
      renderFallback(mount, idBySlug, state);
    }
  }

  // Post-render SVG decoration: route clicks + overlay a true 45° hatch on
  // tainted nodes (the DOT already carries the dashed ring + verdict colors via
  // node style/class, so we only add what graphviz cannot: the hatch pattern).
  function decorate(mount, idBySlug, state) {
    var svg = mount.querySelector("svg");
    if (!svg) return;
    ensureHatchPattern(svg);
    var tainted = {};
    (state.tainted || []).forEach(function (id) { tainted[id] = true; });
    var colors = state.colors || {};

    var nodes = svg.querySelectorAll("g.node");
    nodes.forEach(function (g) {
      var titleEl = g.querySelector("title");
      var slug = titleEl ? titleEl.textContent.trim() : "";
      var id = idBySlug[slug];
      if (!id) return;
      g.style.cursor = "pointer";
      g.addEventListener("click", function () {
        window.location.href = "/node/" + encodeURIComponent(id);
      });
      // A blue (in-Mathlib) node is trusted by construction — never hatch it.
      if (tainted[id] && colors[id] !== "in_mathlib") {
        var shape = g.querySelector("ellipse, polygon, path");
        if (shape) {
          // Layer the hatch over the existing verdict fill.
          var clone = shape.cloneNode(true);
          clone.setAttribute("fill", "url(#rv-hatch)");
          clone.setAttribute("fill-opacity", "0.45");
          clone.setAttribute("stroke", "none");
          shape.parentNode.appendChild(clone);
        }
      }
    });
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

  // Static, dependency-free fallback: list every node colored by its **trust
  // state** (blue = in Mathlib, else the effective verdict), each linking to its
  // packet. Tainted nodes get a hatch chip — except blue nodes, which are trusted
  // by construction and never hatched or shown as AI-only.
  function renderFallback(mount, idBySlug, state) {
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
