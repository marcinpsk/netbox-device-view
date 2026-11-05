// Zoom controls of the artwork panels: each face zooms inside its own viewport.
(function () {
  if (window.dvArtworkZoom) return;
  window.dvArtworkZoom = true;
  var MAX = 8;
  var STEP = 1.5;

  function zoom(viewport, action) {
    var current = parseFloat(viewport.dataset.zoom) || 1;
    var next = action === "in" ? current * STEP : action === "out" ? current / STEP : 1;
    next = Math.min(MAX, Math.max(1, next));
    // Keep the centre of the view in place.
    var cx = (viewport.scrollLeft + viewport.clientWidth / 2) / viewport.scrollWidth;
    var cy = (viewport.scrollTop + viewport.clientHeight / 2) / viewport.scrollHeight;
    viewport.dataset.zoom = String(next);
    viewport.style.setProperty("--dv-zoom", String(next));
    viewport.scrollLeft = cx * viewport.scrollWidth - viewport.clientWidth / 2;
    viewport.scrollTop = cy * viewport.scrollHeight - viewport.clientHeight / 2;
  }

  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-dv-zoom]");
    if (!button) return;
    var panel = button.closest(".dv-art-panel");
    var viewport = panel && panel.querySelector(".dv-art-viewport");
    if (viewport) zoom(viewport, button.getAttribute("data-dv-zoom"));
  });
})();

// Assembly navigation: a navigation item or a breadcrumb shows a view; Escape goes back to the parent view.
(function () {
  if (window.dvArtworkFocus) return;
  window.dvArtworkFocus = true;
  // {view id: the element that had the focus when the view was left}
  var lastFocus = {};

  function shown(part) {
    return part.querySelector(".dv-art-view:not([hidden])");
  }

  // The body has the focus when no element has it, for example after a click in Safari.
  function visible(element) {
    if (!element || element === document.body || element === document.documentElement) return false;
    return element.isConnected && element.getClientRects().length > 0;
  }

  function inFocus(part) {
    var view = shown(part);
    return view !== null && view.hasAttribute("data-dv-parent");
  }

  // A hidden element loses its scroll position, so keep it on the viewport.
  function keepScroll(view) {
    view.querySelectorAll(".dv-art-viewport").forEach(function (viewport) {
      viewport.dataset.scroll = viewport.scrollLeft + " " + viewport.scrollTop;
    });
  }

  function restoreScroll(view) {
    view.querySelectorAll(".dv-art-viewport[data-scroll]").forEach(function (viewport) {
      var scroll = viewport.dataset.scroll.split(" ");
      viewport.scrollLeft = Number(scroll[0]);
      viewport.scrollTop = Number(scroll[1]);
    });
  }

  function show(part, ident) {
    var from = shown(part);
    var to = document.getElementById(ident);
    if (!from || !to || from === to || !part.contains(to)) return false;
    lastFocus[from.id] = document.activeElement;
    keepScroll(from);
    from.hidden = true;
    to.hidden = false;
    restoreScroll(to);
    part.querySelectorAll(".dv-art-nav [data-dv-focus]").forEach(function (item) {
      if (item.getAttribute("data-dv-focus") === ident) item.setAttribute("aria-current", "true");
      else item.removeAttribute("aria-current");
    });
    return true;
  }

  // Go back to a view above the shown one, and give the focus back to where it was in that view.
  function back(part, ident) {
    if (!show(part, ident)) return;
    var previous = lastFocus[ident];
    if (!visible(previous)) previous = part.querySelector('.dv-art-nav [data-dv-focus="' + ident + '"]');
    if (visible(previous)) previous.focus();
  }

  document.addEventListener("click", function (event) {
    var item = event.target.closest("[data-dv-focus]");
    var part = item && item.closest("[data-dv-assembly]");
    if (!part) return;
    if (item.closest(".dv-art-breadcrumb")) back(part, item.getAttribute("data-dv-focus"));
    else show(part, item.getAttribute("data-dv-focus"));
  });

  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape" || event.defaultPrevented) return;
    // The details dialog closes first.
    if (document.querySelector("dialog[open]")) return;
    var part = event.target.closest && event.target.closest("[data-dv-assembly]");
    // After a click on a breadcrumb the focus is on the body; with one focus view shown, Escape still goes back.
    if (!part && (event.target === document.body || event.target === document.documentElement)) {
      var open = Array.prototype.filter.call(document.querySelectorAll("[data-dv-assembly]"), inFocus);
      if (open.length === 1) part = open[0];
    }
    if (!part || !inFocus(part)) return;
    event.preventDefault();
    back(part, shown(part).getAttribute("data-dv-parent"));
  });
})();
