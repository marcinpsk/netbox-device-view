# Troubleshooting

## Tab not showing / badge count is zero

No DeviceView exists for this device's type.

Go to *Plugins → Device Views → Add* and create one for the device type.

---

## A port is not appearing in the grid

The port's `stylename` is not referenced in the layout.

Check what stylename the plugin derives — it follows the rules in [How It Works](how-it-works.md). Then add a matching area name to your YAML or CSS layout.

---

## Layout looks wrong after adding a module

Add a `variants:` block to your YAML layout (or a `.deviceview.module{ModelName}.area` rule in CSS) that includes the new module ports. See [YAML Layout — Variant definition](layout-yaml.md#variant-definition).

---

## Virtual chassis ports missing

A member whose device type has no active artwork and no DeviceView that you can view shows as a generated schematic. Configure a DeviceView or activate artwork for its device type to show its layout or artwork. The plugin applies the CSS scoping (`.d{vc_position}`) automatically, so you do not need to scope the CSS yourself.

---

## SVG mode shows nothing / falls back to CSS

SVG mode requires:

1. The DeviceView has **Render Mode → SVG** set
2. The DeviceView has a non-empty **YAML Layout** field

For a virtual chassis, the DeviceView of the device type of each member selects the mode of that member.

If any of these conditions are not met, the plugin silently falls back to CSS rendering.

---

## "The device view cannot be shown because its YAML layout does not parse"

The stored **YAML Layout** of the device type has an error. The message shows the error and its location in the layout. For a virtual chassis, the message shows under the label of the member whose device type has the layout. The other members still show.

Edit the DeviceView and correct the layout. When you save a DeviceView, the plugin checks its YAML layout. The form, the bulk import and the REST API reject a layout that does not parse, so only a layout that was saved before this check can show this message.

---

## Port status colours not showing in SVG mode

The server writes the status classes (`bg-success`, `bg-warning`, `bg-secondary`, `bg-danger`) into the SVG. The colours of these classes come from a static file. Check that the static file `netbox_device_view/css/device_view_svg.css` has been collected:

```bash
python manage.py collectstatic --no-input
```

A grey, dashed port is an ambiguous stylename: two or more components share it. See [Shared stylenames](how-it-works.md#shared-stylenames).

---

## Partial connection dot not visible

The server adds the yellow dot when an interface has `link_peers` but no `connected_endpoints`. Verify that:

- The port's cable is actually attached in NetBox
- The cable path genuinely has no far-end device (check via the port's trace view)
