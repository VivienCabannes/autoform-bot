(function () {
  "use strict";

  var stateUrl = window.AUTOFORM_STATE_URL || "data/state.json";
  var cards = Array.prototype.slice.call(document.querySelectorAll(".af-node-card"));
  var filter = document.getElementById("af-filter");
  var tierButtons = Array.prototype.slice.call(document.querySelectorAll(".af-tier-filter button"));
  var activeTier = "all";
  var state = null;

  function applyFilters() {
    var query = filter ? filter.value.trim().toLowerCase() : "";
    cards.forEach(function (card) {
      var tierMatches = activeTier === "all" || card.getAttribute("data-tier") === activeTier;
      var textMatches = !query || (card.getAttribute("data-search") || "").indexOf(query) !== -1;
      card.hidden = !(tierMatches && textMatches);
    });
  }

  tierButtons.forEach(function (button) {
    button.setAttribute("aria-pressed", button.getAttribute("data-tier") === "all" ? "true" : "false");
    button.addEventListener("click", function () {
      activeTier = button.getAttribute("data-tier") || "all";
      tierButtons.forEach(function (item) {
        item.setAttribute("aria-pressed", item === button ? "true" : "false");
      });
      applyFilters();
    });
  });
  if (filter) filter.addEventListener("input", applyFilters);

  fetch(stateUrl, { credentials: "same-origin" })
    .then(function (response) {
      if (!response.ok) throw new Error("snapshot unavailable");
      return response.json();
    })
    .then(function (payload) {
      state = payload;
      document.documentElement.setAttribute("data-autoform-snapshot", String(state.schema_version));
    })
    .catch(function () {
      document.documentElement.setAttribute("data-autoform-snapshot", "embedded-navigation-only");
    });
})();
