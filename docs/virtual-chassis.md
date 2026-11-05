# Virtual Chassis

When a device is part of a NetBox Virtual Chassis, the plugin shows each member as a separate panel within the same device view page.

---

## How it works

For Virtual Chassis devices:

- The plugin shows the members that you can view in rack order: top of the rack first, unracked members last. A member that you cannot view has no panel and no label.
- Each member has a label: its name, which links to its own Device View tab, and its position, for example `sw1 · position 1`. The label and the panel of the device that you opened are marked.
- Each member resolves alone. Active artwork of its device type comes first. Else its DeviceView draws it: an SVG panel in SVG mode with a YAML layout, else a CSS grid. A member whose YAML layout does not parse shows the parse error; the other members still show.
- A member with no artwork and no DeviceView that you can view shows a generated schematic: its components by type in NetBox name order, and a slot for each module bay. A slot shows its installed module (its artwork scaled to fit, or its schematic), or "not recorded".
- When no member that you can view has artwork or a DeviceView, the device has no device view.

---

## CSS scoping

For the CSS renderer, each member's grid-area selectors are automatically rewritten:

```css
/* Member at vc_position=1 */
.deviceview.area.d1 {
    grid-template-areas: "...";
}

/* Member at vc_position=2 */
.deviceview.area.d2 {
    grid-template-areas: "...";
}
```

This happens automatically. You do not need to write scoped CSS yourself. The plugin applies the `.d{pos}` rewrite at render time using the base DeviceView CSS for each member's device type. Each grid gets the height of its own device type.

---

## Limitations

- In CSS mode, a member grid shows no front or rear ports, because one grid per member cannot hold both faces.
- The [rendered-layout API](rendered-layout-api.md#virtual-chassis) keeps its own rule: it needs an SVG-capable DeviceView for every member. The [artwork API](artwork-api.md) resolves each member as the page does.
