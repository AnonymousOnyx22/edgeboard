/* Edgeboard browser behaviour.
 *
 * Everything here degrades safely: with JavaScript disabled the pages still
 * render, the nav is expanded, and revealed content is visible (the no-js
 * fallback at the bottom of this file handles that).
 */
(function () {
  "use strict";

  var reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var body = document.body;

  // Tells the head failsafe that this script ran, so it leaves the reveal
  // styles in place instead of stripping them.
  document.documentElement.setAttribute("data-reveal-ready", "");

  /* ----------------------------------------------------------------------
   * Collapsible navigation
   * Desktop collapses the rail to icons; mobile slides it in as a drawer.
   * The desktop choice is remembered, because re-collapsing on every page
   * load would be a chore.
   * -------------------------------------------------------------------- */

  var STORE_KEY = "edgeboard:nav";
  var navToggle = document.getElementById("navToggle");
  var menuButton = document.getElementById("menuButton");
  var scrim = document.getElementById("scrim");
  var sidebar = document.getElementById("sidebar");

  function readStoredNav() {
    try {
      return localStorage.getItem(STORE_KEY);
    } catch (err) {
      return null; // private mode, blocked storage - not worth failing over
    }
  }

  function storeNav(value) {
    try {
      localStorage.setItem(STORE_KEY, value);
    } catch (err) {
      /* ignore */
    }
  }

  if (readStoredNav() === "collapsed") {
    body.setAttribute("data-nav", "collapsed");
  }

  if (navToggle) {
    navToggle.addEventListener("click", function () {
      var collapsed = body.getAttribute("data-nav") === "collapsed";
      var next = collapsed ? "expanded" : "collapsed";
      body.setAttribute("data-nav", next);
      navToggle.setAttribute("aria-expanded", String(collapsed));
      storeNav(next);
    });
  }

  function setDrawer(open) {
    body.setAttribute("data-drawer", open ? "open" : "closed");
    if (menuButton) menuButton.setAttribute("aria-expanded", String(open));
    if (sidebar) sidebar.setAttribute("aria-hidden", String(!open && isMobile()));
  }

  function isMobile() {
    return window.matchMedia("(max-width: 760px)").matches;
  }

  if (menuButton) {
    menuButton.addEventListener("click", function () {
      setDrawer(body.getAttribute("data-drawer") !== "open");
    });
  }

  if (scrim) scrim.addEventListener("click", function () { setDrawer(false); });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && body.getAttribute("data-drawer") === "open") {
      setDrawer(false);
    }
  });

  // Following a link inside the drawer should close it on the way out.
  document.querySelectorAll(".nav-item").forEach(function (link) {
    link.addEventListener("click", function () {
      if (isMobile()) setDrawer(false);
    });
  });

  /* ----------------------------------------------------------------------
   * Sticky topbar
   * The bar only takes on a background once content has scrolled beneath it,
   * so the top of the page stays clean.
   * -------------------------------------------------------------------- */

  var topbar = document.querySelector(".topbar");
  if (topbar) {
    var sentinel = document.createElement("div");
    sentinel.setAttribute("aria-hidden", "true");
    sentinel.style.cssText = "position:absolute;top:0;height:1px;width:1px;";
    body.appendChild(sentinel);

    if ("IntersectionObserver" in window) {
      new IntersectionObserver(function (entries) {
        topbar.classList.toggle("is-stuck", !entries[0].isIntersecting);
      }).observe(sentinel);
    }
  }

  /* ----------------------------------------------------------------------
   * Scroll reveal
   * Sections and list items are tagged automatically, so templates stay
   * clean. Each group staggers its own children.
   * -------------------------------------------------------------------- */

  var REVEAL_SELECTOR = [
    ".hero > *", ".metric", ".card", ".section-head", ".game-card",
    ".parlay", ".chart", ".table-wrap", ".empty", ".settings-panel",
    ".filters"
  ].join(",");

  var groups = [
    ".metric-grid", ".card-grid", ".games", ".parlay-list",
    ".settings-grid", ".hero"
  ];

  document.querySelectorAll(REVEAL_SELECTOR).forEach(function (el) {
    if (!el.hasAttribute("data-reveal")) el.setAttribute("data-reveal", "");
  });

  // Stagger index is scoped per group, and capped so long slates do not
  // leave the final card waiting seconds to appear.
  groups.forEach(function (selector) {
    document.querySelectorAll(selector).forEach(function (group) {
      var index = 0;
      Array.prototype.forEach.call(group.children, function (child) {
        if (child.hasAttribute("data-reveal")) {
          child.style.setProperty("--reveal-index", Math.min(index, 6));
          index += 1;
        }
      });
    });
  });

  var revealTargets = document.querySelectorAll("[data-reveal]");

  if (reduceMotion || !("IntersectionObserver" in window)) {
    revealTargets.forEach(function (el) { el.classList.add("is-visible", "is-settled"); });
  } else {
    var revealObserver = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        entry.target.classList.add("is-visible");
        revealObserver.unobserve(entry.target);
        // Release the compositor hint once the transition has finished.
        window.setTimeout(function () {
          entry.target.classList.add("is-settled");
        }, 1200);
      });
    }, { rootMargin: "0px 0px -8% 0px", threshold: 0.08 });

    revealTargets.forEach(function (el) { revealObserver.observe(el); });
  }

  /* ----------------------------------------------------------------------
   * Chart line draw-on
   * The polyline draws itself once, the first time the chart is seen.
   * -------------------------------------------------------------------- */

  document.querySelectorAll(".chart-line").forEach(function (line) {
    var length;
    try {
      length = line.getTotalLength();
    } catch (err) {
      return; // not laid out yet, or an empty series
    }
    if (!length || reduceMotion) return;

    line.style.setProperty("--len", length);
    line.classList.add("is-drawable");

    var start = function () {
      // Next frame, so the dash offset is applied before it transitions.
      requestAnimationFrame(function () { line.classList.add("is-drawn"); });
    };

    if ("IntersectionObserver" in window) {
      var chartObserver = new IntersectionObserver(function (entries) {
        entries.forEach(function (entry) {
          if (!entry.isIntersecting) return;
          start();
          chartObserver.unobserve(entry.target);
        });
      }, { threshold: 0.25 });
      chartObserver.observe(line);
    } else {
      start();
    }
  });

  /* ----------------------------------------------------------------------
   * Metric count-up
   * Only genuinely numeric values animate. Records like "7-3", em dashes and
   * anything else are left exactly as rendered.
   * -------------------------------------------------------------------- */

  var NUMERIC = /^([+-]?)(\d+(?:\.\d+)?)(%|u)?$/;

  function countUp(el) {
    var match = NUMERIC.exec(el.textContent.trim());
    if (!match) return;

    var sign = match[1] === "-" ? -1 : 1;
    var target = parseFloat(match[2]) * sign;
    var suffix = match[3] || "";
    var explicitPlus = match[1] === "+";
    var decimals = (match[2].split(".")[1] || "").length;
    var duration = 800;
    var startedAt = null;

    function frame(now) {
      if (startedAt === null) startedAt = now;
      var progress = Math.min((now - startedAt) / duration, 1);
      // ease-out cubic, matching the CSS curve's character
      var eased = 1 - Math.pow(1 - progress, 3);
      var value = (target * eased).toFixed(decimals);
      el.textContent = (explicitPlus && Number(value) >= 0 ? "+" : "") + value + suffix;
      if (progress < 1) requestAnimationFrame(frame);
    }

    requestAnimationFrame(frame);
  }

  if (!reduceMotion && "IntersectionObserver" in window) {
    var counters = document.querySelectorAll(".metric strong, .parlay-price");
    var counterObserver = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        counterObserver.unobserve(entry.target);
        countUp(entry.target);
      });
    }, { threshold: 0.5 });
    counters.forEach(function (el) { counterObserver.observe(el); });
  }

  /* ----------------------------------------------------------------------
   * Broken crests and headshots
   * ESPN has no image for every team or athlete, and a torn-image icon in a
   * results row looks like a bug. Swap in the monogram instead.
   *
   * The error event does not bubble, so this listens during capture.
   * -------------------------------------------------------------------- */

  document.addEventListener("error", function (event) {
    var img = event.target;
    if (!img || img.tagName !== "IMG") return;
    if (!img.classList.contains("crest") && !img.classList.contains("headshot")) return;

    var fallback = document.createElement("span");
    fallback.className = "avatar-fallback" +
      (img.classList.contains("sm") ? " sm" : "");
    fallback.setAttribute("aria-hidden", "true");
    fallback.textContent = img.dataset.monogram || "?";
    if (img.parentNode) img.parentNode.replaceChild(fallback, img);
  }, true);

  /* ----------------------------------------------------------------------
   * Form submission feedback
   * Syncing and grading take real time, so the button says so and cannot be
   * double-fired.
   * -------------------------------------------------------------------- */

  document.querySelectorAll("form[data-loading]").forEach(function (form) {
    form.addEventListener("submit", function () {
      var button = form.querySelector('button[type="submit"], button:not([type])');
      if (!button || button.disabled) return;
      button.dataset.oldText = button.textContent;
      button.disabled = true;
      button.textContent = "Working…";
    });
  });
})();
