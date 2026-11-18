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
    // Keep the point of the face at the centre of the view; the face can be narrower than the view.
    var face = viewport.querySelector(".dv-art-face");
    var before = face.getBoundingClientRect();
    var centre = centreOf(viewport);
    var x = (centre.x - before.left) / before.width;
    var y = (centre.y - before.top) / before.height;
    viewport.dataset.zoom = String(next);
    viewport.style.setProperty("--dv-zoom", String(next));
    var after = face.getBoundingClientRect();
    // A scroll bar can appear, so the centre is measured again.
    centre = centreOf(viewport);
    viewport.scrollLeft += after.left + x * after.width - centre.x;
    viewport.scrollTop += after.top + y * after.height - centre.y;
  }

  function centreOf(viewport) {
    var box = viewport.getBoundingClientRect();
    return {
      x: box.left + viewport.clientLeft + viewport.clientWidth / 2,
      y: box.top + viewport.clientTop + viewport.clientHeight / 2,
    };
  }

  // A face at zoom 1 is as wide as the panel, but not taller than the panel's maximum height.
  document.querySelectorAll(".dv-art-face").forEach(function (face) {
    var box = face.viewBox.baseVal;
    if (box && box.width > 0 && box.height > 0) {
      face.style.setProperty("--dv-fit", "min(100%, var(--dv-max-height) * " + box.width / box.height + ")");
    }
  });

  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-dv-zoom]");
    if (!button) return;
    var panel = button.closest(".dv-art-panel");
    var viewport = panel && panel.querySelector(".dv-art-viewport");
    if (viewport) zoom(viewport, button.getAttribute("data-dv-zoom"));
  });
})();
