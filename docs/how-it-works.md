# How It Works

For each **Device Type** you create a **DeviceView** object (at *Plugins → Device Views → Add*) with either a [YAML layout](layout-yaml.md) or a legacy [CSS layout](layout-css.md).

When you visit a device of that type, the plugin:

1. Looks up the DeviceView for the device's type
2. Collects all interfaces, front ports, rear ports, console ports, and console-server ports from the device, including the ports of installed modules. It uses only the objects that you can view (see [Object permissions](#object-permissions))
3. Derives a `stylename` for each port (see below) and binds each layout element to the components with the same stylename (see [Shared stylenames](#shared-stylenames))
4. Finds the connection state of each port on the server
5. Renders the layout using the configured render mode (CSS Grid or SVG), with the port status colours

The Device View tab, the device page panel and the [rendered-layout API](rendered-layout-api.md) use one module (`netbox_device_view.components`) for steps 2 to 4. Thus the page and the API show the same component in the same state.

---

## Port / Interface name → stylename

Each interface and port is mapped to a `stylename` used as the CSS grid-area name (and SVG `data-stylename` attribute). The derivation rules are:

| Interface name | Stylename |
|---|---|
| `GigabitEthernet0/0/1` | `gigabitethernet0-1` |
| `TenGigabitEthernet1/1/4` | `tengigabitethernet1-4` |
| `GigabitEthernet0/1` | `gigabitethernet-1` |
| `Management0` | `management-0` |
| `eth0` | `eth-0` |
| `1` (numeric only) | `p1` |

**Rules:**

- Names are lowercased
- For the 3-part Cisco format `Type{dev}/{module}/{port}`, stylename is `type{module}-{port}`
- For the 2-part format `Type{dev}/{port}`, stylename is `type-{port}`
- All other names: non-alphanumeric/dot characters replaced with `-`
- Front, rear, console and console-server ports always use the replacement rule for other names: `ttyS1` becomes `ttys1`, and `Async0/1/0` becomes `async0-1-0`
- Purely numeric stylenames are prefixed with `p`
- Virtual and LAG interfaces are skipped

### Shared stylenames

Two components can derive the same stylename, for example an interface `Console` and a console port `Console`. The plugin does not select one of them:

- The element shows a neutral, dashed cell. It links to no component.
- Its popover lists each component with a link.
- The [rendered-layout API](rendered-layout-api.md#ambiguous-hotspot) gives one hotspot with `connection_state: "ambiguous"` and a `candidates` list.

A front port binds only on the front face, and a rear port only on the rear face. Thus the front port `Port 1` and the rear port `Port 1` of a patch panel do not share a stylename when the layout has separate `front` and `rear` views. A layout with only a `rear` view draws the rear face; any other layout with one view draws the front face.

In CSS mode, a device outside a virtual chassis shows its front ports and rear ports in separate **Front** and **Rear** grids. A virtual chassis member shows no front or rear ports in CSS mode, because one grid per member cannot hold both faces.

---

## Render modes

A DeviceView has a **Render Mode** field:

| Mode | Description |
|---|---|
| `css` (default) | CSS Grid renderer — works with both YAML and legacy CSS layouts |
| `svg` | SVG renderer — requires a YAML layout; produces a scalable `<svg>` element |

See [SVG Renderer](svg-renderer.md) for full details on SVG mode.

---

## Find device views

The Device Views list (*Plugins → Device Views*) has a quick search and a **Filters** tab. The quick search finds the device views whose device type model or manufacturer name contains the text.

The REST API (`GET /api/plugins/device_view/device-view/`) takes the same filters:

| Parameter | Filters by |
|---|---|
| `q` | device type model or manufacturer name (contains, case-insensitive) |
| `device_type_id`, `device_type` | device type ID or slug |
| `manufacturer_id`, `manufacturer` | manufacturer ID or slug |
| `id`, `tag` | device view ID or tag slug |

---

## Object permissions

The Device View tab and the device page panel show only the objects that you can view. NetBox object permissions and their constraints apply to each object:

- The Device View tab of a device that you cannot view is not found (`404`).
- A virtual chassis member that you cannot view has no panel. The panel title shows the virtual chassis name only when you can view the virtual chassis.
- A DeviceView that you cannot view counts as missing. The device page then has no Device View tab.
- A port that you cannot view is not in the panel. A peer that you cannot view is not in the port's popover.
- A cable that you cannot view counts as no cable: it gives no cable colour, and the port shows the state of a port without a cable. A peer that you can view is still in the popover.
- A module that you cannot view does not select a module variant, and its ports are not in the panel.

The tab needs the **view** permission on **Device View**. The [rendered-layout API](rendered-layout-api.md#object-permissions) uses the same rule.
