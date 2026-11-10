/* Edgeboard browser behaviour.
 *
 * Everything here degrades safely: with JavaScript disabled the pages still
 * render, the nav is expanded, and the crawl is a strip you can scroll.
 * Nothing on a page is ever hidden waiting for a script or a scroll.
 */
(function () {
  "use strict";

  var reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var hasObserver = "IntersectionObserver" in window;
  var body = document.body;
  var root = document.documentElement;

  function onceVisible(elements, callback, options) {
    if (!hasObserver) {
      Array.prototype.forEach.call(elements, callback);
      return;
    }
    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        observer.unobserve(entry.target);
        callback(entry.target);
      });
    }, options || { rootMargin: "0px 0px -8% 0px", threshold: 0.08 });
    Array.prototype.forEach.call(elements, function (el) { observer.observe(el); });
  }

  /* ----------------------------------------------------------------------
   * Collapsible navigation
   * Desktop collapses the rail to icons; mobile slides it in as a drawer.
   * The desktop choice is remembered, because collapsing it again on every
   * page load would be a chore.
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
      return null; // private mode or blocked storage; not worth failing over
    }
  }

  function storeNav(value) {
    try {
      localStorage.setItem(STORE_KEY, value);
    } catch (err) {
      /* ignore */
    }
  }

  function isMobile() {
    return window.matchMedia("(max-width: 760px)").matches;
  }

  if (readStoredNav() === "collapsed") {
    body.setAttribute("data-nav", "collapsed");
    if (navToggle) navToggle.setAttribute("aria-expanded", "false");
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
    if (menuButton) {
      menuButton.setAttribute("aria-expanded", String(open));
      menuButton.setAttribute("aria-label", open ? "Close navigation" : "Open navigation");
    }
    if (sidebar) sidebar.setAttribute("aria-hidden", String(!open && isMobile()));
    if (open && sidebar) {
      var first = sidebar.querySelector(".nav-item");
      if (first) first.focus();
    }
  }

  if (sidebar && isMobile()) sidebar.setAttribute("aria-hidden", "true");

  if (menuButton) {
    menuButton.addEventListener("click", function () {
      setDrawer(body.getAttribute("data-drawer") !== "open");
    });
  }

  if (scrim) scrim.addEventListener("click", function () { setDrawer(false); });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && body.getAttribute("data-drawer") === "open") {
      setDrawer(false);
      if (menuButton) menuButton.focus();
    }
  });

  // Following a link inside the drawer should close it on the way out.
  document.querySelectorAll(".nav-item").forEach(function (link) {
    link.addEventListener("click", function () {
      if (isMobile()) setDrawer(false);
    });
  });

  window.addEventListener("resize", function () {
    if (!isMobile() && sidebar) {
      sidebar.removeAttribute("aria-hidden");
      body.setAttribute("data-drawer", "closed");
    }
  });

  /* ----------------------------------------------------------------------
   * Sticky topbar
   * The bar takes on a seam only once content has scrolled beneath it.
   * -------------------------------------------------------------------- */

  var topbar = document.querySelector(".topbar");
  if (topbar && hasObserver) {
    var sentinel = document.createElement("div");
    sentinel.setAttribute("aria-hidden", "true");
    sentinel.style.cssText = "position:absolute;top:0;height:1px;width:1px;";
    body.appendChild(sentinel);
    new IntersectionObserver(function (entries) {
      topbar.classList.toggle("is-stuck", !entries[0].isIntersecting);
    }).observe(sentinel);
  }

  /* ----------------------------------------------------------------------
   * The crawl
   * The list is cloned once so translating it by half loops seamlessly, and
   * the duration comes from its width so it always reads at the same pace.
   * A crawl that already fits stays still.
   * -------------------------------------------------------------------- */

  var crawl = document.querySelector(".crawl");
  var crawlList = document.getElementById("crawlList");
  if (crawl && crawlList && !reduceMotion) {
    var viewport = crawl.querySelector(".crawl-viewport");
    var startCrawl = function () {
      var width = crawlList.scrollWidth;
      if (!viewport || width <= viewport.clientWidth) return;
      Array.prototype.slice.call(crawlList.children).forEach(function (item) {
        var copy = item.cloneNode(true);
        copy.setAttribute("aria-hidden", "true");
        copy.querySelectorAll("a").forEach(function (a) { a.setAttribute("tabindex", "-1"); });
        crawlList.appendChild(copy);
      });
      var pixelsPerSecond = 48;
      crawl.style.setProperty("--crawl-duration", Math.max(20, width / pixelsPerSecond) + "s");
      crawl.classList.add("is-running");
    };
    if (document.fonts && document.fonts.ready) {
      document.fonts.ready.then(startCrawl);
    } else {
      startCrawl();
    }
  }

  /* ----------------------------------------------------------------------
   * Counting figures
   * Only plain numbers run: 58%, +3.9u, −1.2, 412. Anything with other text
   * in it is left exactly as rendered.
   * -------------------------------------------------------------------- */

  var NUMERIC = /^([+\-−]?)(\$?)(\d+(?:\.\d+)?)(%|u)?$/;

  function countUp(el) {
    var match = NUMERIC.exec(el.textContent.trim());
    if (!match) return;
    var negative = match[1] === "-" || match[1] === "−";
    var minus = match[1] === "-" ? "-" : "−";
    var target = parseFloat(match[3]);
    var money = match[2];
    var suffix = match[4] || "";
    var explicitPlus = match[1] === "+";
    var decimals = (match[3].split(".")[1] || "").length;
    var duration = 900;
    var startedAt = null;

    function frame(now) {
      if (startedAt === null) startedAt = now;
      var progress = Math.min((now - startedAt) / duration, 1);
      var eased = 1 - Math.pow(1 - progress, 4);
      var value = (target * eased).toFixed(decimals);
      var sign = negative ? minus : (explicitPlus ? "+" : "");
      el.textContent = sign + money + value + suffix;
      if (progress < 1) requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);
  }

  if (!reduceMotion) {
    onceVisible(document.querySelectorAll(".metric strong, [data-count]"), countUp,
                { threshold: 0.5 });
  }

  /* ----------------------------------------------------------------------
   * Broken crests and headshots
   * ESPN has no image for every team or athlete, and a torn image icon in a
   * row looks like a bug. Swap in the monogram instead. The error event does
   * not bubble, so this listens during capture.
   * -------------------------------------------------------------------- */

  document.addEventListener("error", function (event) {
    var img = event.target;
    if (!img || img.tagName !== "IMG") return;
    if (!img.classList.contains("crest") && !img.classList.contains("headshot")) return;
    var size = ["sm", "lg"].filter(function (s) { return img.classList.contains(s); })[0];
    var fallback = document.createElement("span");
    fallback.className = "avatar-fallback" + (size ? " " + size : "");
    fallback.setAttribute("aria-hidden", "true");
    fallback.textContent = img.dataset.monogram || "?";
    if (img.parentNode) img.parentNode.replaceChild(fallback, img);
  }, true);

  /* ----------------------------------------------------------------------
   * Form submission feedback
   * Syncing and grading take real time, so the button says so and cannot be
   * pressed twice.
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
