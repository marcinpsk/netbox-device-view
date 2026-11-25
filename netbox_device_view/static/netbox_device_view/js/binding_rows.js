// Add a binding row to the override form from the empty row of the formset.
(() => {
  for (const button of document.querySelectorAll("[data-dv-add-binding]")) {
    const group = button.closest(".field-group");
    const body = group.querySelector("[data-dv-binding-rows]");
    const template = group.querySelector("template[data-dv-binding-row]");
    const total = group.querySelector('input[name$="-TOTAL_FORMS"]');
    const update = () => {
      button.disabled = Number(total.value) >= Number(button.dataset.dvMax);
    };
    button.addEventListener("click", () => {
      const index = Number(total.value);
      const row = template.content.firstElementChild.cloneNode(true);
      for (const field of row.querySelectorAll("[name], [id], [for]")) {
        for (const name of ["name", "id", "for"]) {
          const value = field.getAttribute(name);
          if (value) field.setAttribute(name, value.replace("__prefix__", index));
        }
      }
      body.append(row);
      total.value = index + 1;
      update();
      row.querySelector("select").focus();
    });
    update();
  }
})();
