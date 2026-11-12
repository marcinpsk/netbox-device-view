// Connector details: the Details toggle, the Space key, or a connector without one component opens the dialog.
(function () {
  if (window.dvDetails) return;
  window.dvDetails = true;
  // Artwork connectors and CSS layout cells have data-dv-details; an SVG layout port without a component has none.
  var CONNECTOR = "[data-dv-details], .dv-port";
  var returnFocus = null;

  function dialog() {
    return document.querySelector("dialog.dv-details");
  }

  function link(connector) {
    return connector.getAttribute("data-href") || connector.getAttribute("href");
  }

  function detailsMode(connector) {
    var scope = connector.closest("[data-dv-details-scope]");
    return scope !== null && scope.classList.contains("dv-details-on");
  }

  function open(connector) {
    var box = dialog();
    if (!box) return;
    var title = box.querySelector("#dv-details-title");
    var body = box.querySelector(".dv-details-body");
    var ident = connector.getAttribute("data-dv-details");
    var source = ident && document.getElementById(ident);
    body.replaceChildren();
    if (source) {
      title.textContent = source.getAttribute("data-title");
      body.appendChild(source.content.cloneNode(true));
    } else {
      var key = connector.getAttribute("data-stylename") || "";
      title.textContent = key;
      body.appendChild(document.getElementById("dv-details-missing").content.cloneNode(true));
      body.querySelector("[data-dv-details-key]").textContent = key;
    }
    returnFocus = connector;
    box.showModal();
  }

  function follow(connector, event) {
    // An SVG layout port is a group with data-href; a link follows itself.
    if (connector.hasAttribute("data-href")) {
      event.preventDefault();
      window.location.href = link(connector);
    }
  }

  document.addEventListener("click", function (event) {
    var toggle = event.target.closest("[data-dv-details-mode]");
    if (toggle) {
      var on = toggle.getAttribute("aria-pressed") !== "true";
      toggle.setAttribute("aria-pressed", String(on));
      toggle.classList.toggle("active", on);
      var scope = toggle.closest("[data-dv-details-scope]");
      scope.classList.toggle("dv-details-on", on);
      // While the mode is on, a link opens the dialog too.
      scope.querySelectorAll(CONNECTOR).forEach(function (connector) {
        if (!link(connector)) return;
        if (on) connector.setAttribute("aria-haspopup", "dialog");
        else connector.removeAttribute("aria-haspopup");
      });
      return;
    }
    var box = event.target.closest("dialog.dv-details");
    if (box) {
      // The content fills the dialog, so a click on the dialog itself is on the backdrop.
      if (event.target === box || event.target.closest("[data-dv-details-close]")) box.close();
      return;
    }
    var connector = event.target.closest(CONNECTOR);
    if (!connector) return;
    if (link(connector) && !detailsMode(connector)) {
      follow(connector, event);
      return;
    }
    event.preventDefault();
    open(connector);
  });

  document.addEventListener("keydown", function (event) {
    if (event.key !== "Enter" && event.key !== " ") return;
    var connector = event.target.closest && event.target.closest(CONNECTOR);
    if (!connector || connector !== event.target) return;
    if (event.key === "Enter" && link(connector) && !detailsMode(connector)) {
      follow(connector, event);
      return;
    }
    event.preventDefault();
    open(connector);
  });

  document.addEventListener(
    "close",
    function (event) {
      if (!event.target.matches("dialog.dv-details") || returnFocus === null) return;
      returnFocus.focus();
      returnFocus = null;
    },
    true
  );
})();
