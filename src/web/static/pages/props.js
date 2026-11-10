/* Hit rate grid: filter, sort and search in the browser.
 *
 * Every row is rendered by the server with its figures in data attributes,
 * so the grid works without this script; with it, filtering and sorting are
 * instant. The filter state is kept per league for the session, so the
 * page's own refresh (while full game logs load) does not lose it.
 */
(function () {
  "use strict";

  var table = document.getElementById("hrTable");
  var tools = document.getElementById("hrTools");
  if (!table || !tools) return;

  var tbody = table.tBodies[0];
  var rows = Array.prototype.slice.call(tbody.rows);
  var count = document.getElementById("hrCount");
  var empty = document.getElementById("hrEmpty");
  var search = document.getElementById("hrSearch");
  var team = document.getElementById("hrTeam");
  var min = document.getElementById("hrMin");
  var priced = document.getElementById("hrPriced");
  var storeKey = "edgeboard:props:" + (tools.dataset.sport || "");

  // Real sportsbook lines lead whenever the board has any. The model's own
  // lines are chosen to clear, so ranking them first flatters every player.
  var hasBookLines = !!document.querySelector('[data-priced="1"]');
  var state = { q: "", stat: "", side: "", team: "", min: 0, priced: hasBookLines,
                sort: "l10", dir: "desc" };

  try {
    var saved = JSON.parse(sessionStorage.getItem(storeKey) || "null");
    if (saved) Object.keys(state).forEach(function (k) {
      if (k in saved) state[k] = saved[k];
    });
  } catch (err) { /* storage blocked: start fresh */ }

  function save() {
    try { sessionStorage.setItem(storeKey, JSON.stringify(state)); } catch (err) { /* ignore */ }
  }

  function press(group, attr, value) {
    tools.querySelectorAll(group + " [" + attr + "]").forEach(function (button) {
      button.setAttribute("aria-pressed", String(button.getAttribute(attr) === value));
    });
  }

  function value(row, key, type) {
    var raw = row.dataset[key];
    if (type === "text") return (raw || "").toLowerCase();
    var n = parseFloat(raw);
    return isNaN(n) ? -1 : n;
  }

  function sortRows() {
    var header = table.querySelector('[data-sort="' + state.sort + '"]');
    var type = header ? header.dataset.type : "";
    var dir = state.dir === "asc" ? 1 : -1;
    rows.sort(function (a, b) {
      var x = value(a, state.sort, type);
      var y = value(b, state.sort, type);
      // Missing figures always sink, whichever way the column runs.
      if (type !== "text") {
        if (x < 0 && y >= 0) return 1;
        if (y < 0 && x >= 0) return -1;
      }
      if (x < y) return -1 * dir;
      if (x > y) return 1 * dir;
      return 0;
    });
    var fragment = document.createDocumentFragment();
    rows.forEach(function (row) { fragment.appendChild(row); });
    tbody.appendChild(fragment);
    table.querySelectorAll("thead th").forEach(function (th) {
      var button = th.querySelector("button");
      if (button && button.dataset.sort === state.sort) {
        th.setAttribute("aria-sort", state.dir === "asc" ? "ascending" : "descending");
      } else {
        th.removeAttribute("aria-sort");
      }
    });
  }

  function apply(animate) {
    var q = state.q.trim().toLowerCase();
    var shown = 0;
    rows.forEach(function (row) {
      var d = row.dataset;
      var keep = (!q || d.player.indexOf(q) !== -1) &&
        (!state.stat || d.stat === state.stat) &&
        (!state.side || d.side === state.side) &&
        (!state.team || d.team === state.team) &&
        (parseInt(d.n, 10) || 0) >= state.min &&
        (!state.priced || d.priced === "1");
      if (keep && row.hidden && animate) {
        row.classList.remove("is-entering");
        void row.offsetWidth; // restart the entrance
        row.classList.add("is-entering");
      }
      row.hidden = !keep;
      if (keep) shown += 1;
    });
    count.textContent = shown + (shown === 1 ? " line" : " lines");
    if (empty) empty.hidden = shown !== 0;
    save();
  }

  // Controls reflect the restored state.
  search.value = state.q;
  team.value = state.team;
  min.value = String(state.min);
  priced.checked = state.priced;
  press(".hr-chips", "data-stat", state.stat);
  press(".hr-toggle", "data-side", state.side);

  search.addEventListener("input", function () { state.q = search.value; apply(true); });
  team.addEventListener("change", function () { state.team = team.value; apply(true); });
  min.addEventListener("change", function () { state.min = parseInt(min.value, 10) || 0; apply(true); });
  priced.addEventListener("change", function () { state.priced = priced.checked; apply(true); });

  tools.querySelectorAll(".hr-chip").forEach(function (chip) {
    chip.addEventListener("click", function () {
      state.stat = chip.dataset.stat;
      press(".hr-chips", "data-stat", state.stat);
      apply(true);
    });
  });
  tools.querySelectorAll(".hr-toggle button").forEach(function (button) {
    button.addEventListener("click", function () {
      state.side = button.dataset.side;
      press(".hr-toggle", "data-side", state.side);
      apply(true);
    });
  });

  table.querySelectorAll("thead button[data-sort]").forEach(function (button) {
    button.addEventListener("click", function () {
      var key = button.dataset.sort;
      if (state.sort === key) {
        state.dir = state.dir === "desc" ? "asc" : "desc";
      } else {
        state.sort = key;
        // Names read A to Z first; figures read best first.
        state.dir = button.dataset.type === "text" ? "asc" : "desc";
      }
      sortRows();
      save();
    });
  });

  // The whole row opens the player, unless a link inside it was the target.
  tbody.addEventListener("click", function (event) {
    if (event.target.closest("a, button")) return;
    var row = event.target.closest("tr[data-href]");
    if (row) window.location.href = row.dataset.href;
  });

  tools.hidden = false;
  sortRows();
  apply(false);

  // While the full game logs load, check back without replaying entrances.
  var pending = document.getElementById("hrPending");
  if (pending) {
    window.setTimeout(function () {
      try { sessionStorage.setItem("edgeboard:switch", "1"); } catch (err) { /* ignore */ }
      window.location.reload();
    }, parseInt(pending.dataset.refresh, 10) || 5000);
  }
})();
