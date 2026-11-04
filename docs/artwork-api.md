# Artwork API

A read-only, versioned JSON endpoint that gives each device of a device or virtual chassis its active artwork with the modules in its bays, its layout panels, or the reason it has neither. The Device View tab uses the same resolution code, so the API and the page draw the same modules and bind the same components in the same state.

The [rendered-layout API](rendered-layout-api.md) does not change. It serves YAML layouts only.

---

## Endpoint

```
GET /api/plugins/device_view/devices/<device_id>/artwork/
```

Access is as for the rendered-layout API: a token, and the **view** permission on the device. Without the permission, the response is `403`. A device outside the permission scope gives `404`, like a device that does not exist.

The artwork needs no permission on the artwork association or the revision. Components, cables, peers, members, modules and module bays need the **view** permission.

---

## Response schema (`schema_version: 1`)

```json
{
  "schema_version": 1,
  "device_id": 12,
  "members": [
    {
      "device_id": 12,
      "label": "sw1",
      "position": 1,
      "current": true,
      "representation": "artwork",
      "reason": null,
      "artwork": {
        "definition_key": "acme/sw-24",
        "revision_id": 5,
        "digest": "3f1c…",
        "faces": [
          {"name": "front", "label": "Front", "svg": "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 440 44\">…</svg>"}
        ],
        "connectors": [
          {
            "key": "con-eth1",
            "face": "front",
            "label": "1",
            "element_id": "dv-a-d12-front-con-eth1",
            "bindings": [
              {
                "status": "found",
                "object_type": "dcim.interface",
                "object_id": 456,
                "name": "eth1",
                "enabled": true,
                "cable_status": "connected",
                "cable_color": "ff0000",
                "marked_connected": false,
                "path_complete": true,
                "peer_labels": ["server1 | eth0"],
                "trace_supported": true
              }
            ]
          }
        ],
        "modules": [
          {
            "module_id": 31,
            "module_bay_id": 40,
            "parent_module_id": null,
            "bay_key": "bay-nm",
            "face": "front",
            "representation": "artwork",
            "artwork": {"definition_key": "acme/nm-2x", "revision_id": 9, "digest": "8a2e…"},
            "placement": {"x": 300, "y": 5, "width": 100, "height": 30, "rotate": 0},
            "element_id": "dv-a-d12-m31",
            "connectors": [
              {"key": "con-p1", "face": "installed", "label": null, "element_id": "dv-a-d12-m31-installed-con-p1", "bindings": […]}
            ],
            "warnings": []
          }
        ]
      },
      "panels": []
    }
  ]
}
```

A client must reject an unknown `schema_version`. A breaking change gives a new version. A new field in an object is not a breaking change.

### Top level

| Field | Type | Notes |
|---|---|---|
| `schema_version` | int | `1`. |
| `device_id` | int | The requested device. |
| `members` | array | One entry per device that the user can view: the device alone, or each member of its virtual chassis in rack order. A member that the user cannot view has no entry. |

### Member

| Field | Type | Notes |
|---|---|---|
| `device_id` | int | The member device. |
| `label` | string | The device name, as the page shows it. |
| `position` | int \| null | The virtual chassis position. `null` for a device that is not in a virtual chassis. |
| `current` | bool | `true` for the requested device. |
| `representation` | string | `"artwork"`, `"layout"`, `"schematic"` or `"unavailable"`. A client must accept `"schematic"`. This version does not return it yet. |
| `reason` | string \| null | Set only for `"unavailable"`: see [Reasons](#reasons). |
| `artwork` | object \| null | Set only for `"artwork"`. |
| `panels` | array | Set only for `"layout"`. Otherwise empty. |

Each member resolves alone. A member without artwork or a layout does not hide the others. The Device View tab shows artwork for a virtual chassis only when every visible member has it; the API gives artwork to each member that has it.

Active artwork comes before a layout. A member gets `"layout"` when its device type has no active artwork and has a YAML `DeviceView` that the user can view. Its `panels` are the [panels of the rendered-layout API](rendered-layout-api.md#panel) for that device alone: the keys are `front` and `rear`, with no member prefix.

### Reasons

| Reason | Condition |
|---|---|
| `no_layout` | No active artwork, and no `DeviceView` that the user can view. |
| `svg_layout_required` | No active artwork, and the `DeviceView` has no YAML layout. |
| `invalid_layout` | No active artwork, and the YAML layout does not parse. |
| `damaged_artwork` | A stored file of the active revision is missing or does not match its digest. Import the revision again to repair it. |
| `artwork_error` | Drawing the active revision failed with an unexpected error, for example a stored file that storage cannot read. The NetBox log has the error. |

### Artwork

| Field | Type | Notes |
|---|---|---|
| `definition_key` | string | The key of the visual definition, for example `acme/sw-24`. |
| `revision_id`, `digest` | int, string | The active revision. |
| `faces` | array | One entry per face in physical order: `name` (`front`, `rear`, …), `label`, and `svg`. |
| `connectors` | array | One entry per connector of the manifest, sorted by key. |
| `modules` | array | One entry per installed module that a face shows: see [Module](#module). |

Each `svg` is the stored face, parsed and written again, with the [installed modules](artwork-bundles.md#module-bays) in its bays. It has only the elements of the [SVG profile](artwork-bundles.md#svg-profile): no links, scripts, classes or filters. Every `id` of the device face has the prefix `dv-a-d<device_id>-<face>-`. Every `id` of a module face has the prefix `dv-a-d<device_id>-m<module_id>-<face>-`, with one `-m<module_id>` part for each module on the placement path, for example `dv-a-d12-m31-m35-installed-` for a transceiver in a network module. Each reference uses the new id. Thus the faces of several members, and two modules of one type, can share one document. The root keeps its `viewBox` and has no `width` or `height`. Each module face is a nested `svg` element that keeps the `viewBox` of the module, in a group with the placement `transform`, so its percentage lengths keep their meaning.

### Connector

| Field | Type | Notes |
|---|---|---|
| `key` | string | The connector key of the manifest. |
| `face` | string | The face that draws the connector. |
| `label` | string \| null | The label of the manifest. |
| `element_id` | string \| null | The `id` of the connector element in the `svg` of its face. Draw state or find a tap target from this element. `null` when the face does not draw the element, for example in a face stored before the import rejected elements in `title` and `desc`. |
| `bindings` | array | One entry per binding, in list order. The list is the one of the manifest, or the replacement list of a [binding override](artwork-bundles.md#binding-overrides). |

The page links a connector to its component only when exactly one binding has `status: "found"`. A client should do the same.

### Binding

| Field | Type | Notes |
|---|---|---|
| `status` | string | `"found"`, or `"missing"` when the device has no component of that type and name that the user can view. A component that the user cannot view is missing. |
| `object_type` | string | `app_label.model_name` of the bound type, for example `"dcim.interface"` or `"dcim.powerport"`. Set for a missing binding too. |
| `object_id` | int \| null | The component. `null` when missing. |
| `name` | string | The component name. For a missing binding, the name in the binding list. For a connector of a module, the name in which `{module}` resolves as NetBox names the components of that module. |
| `enabled` | bool \| null | The administrative state of an interface. `null` for the other types, which have no `enabled` field. |
| `cable_status` | string \| null | The status of the cable that the user can view: `"connected"`, `"planned"` or `"decommissioning"`. `null` without a cable, or with a cable that the user cannot view. |
| `cable_color` | string \| null | As in a [rendered-layout hotspot](rendered-layout-api.md#hotspot). |
| `marked_connected` | bool \| null | The **Mark connected** flag of the component. |
| `path_complete` | bool \| null | `true` when the cable path of the component is complete. `null` for a type without a cable path, for example a front or rear port. A cable that the user cannot view is no cable, so its path gives `false`. |
| `peer_labels`, `trace_supported` | | As in a [rendered-layout hotspot](rendered-layout-api.md#hotspot). |

Each fact is its own field, and no field combines them. A disabled interface with a cable has `enabled: false` and a `cable_status`. A missing binding has `null` for each fact, `peer_labels: []` and `trace_supported: false`.

### Module

| Field | Type | Notes |
|---|---|---|
| `module_id`, `module_bay_id` | int, int | The installed module and its module bay. |
| `parent_module_id` | int \| null | The module that has the bay. `null` for a bay of the device. |
| `bay_key` | string | The bay key in the manifest of the parent. |
| `face` | string | The face of the device that shows the module. |
| `representation` | string | `"artwork"`, or `"outline"`: an outline of the bay with the model of the module type, when the module type has no active artwork, a stored file of its revision is damaged, the placement is not a finite number, or the module is too deep. A client must accept `"schematic"`: a later version can draw a schematic of the module and its components. |
| `artwork` | object \| null | `definition_key`, `revision_id` and `digest` of the active revision of the module type. `null` without one. |
| `placement` | object \| null | The bay in the coordinates of the device face: `x`, `y`, `width`, `height`, and `rotate`, the rotation of the module face in degrees. `null` when the bay or the placement is not a finite number. |
| `element_id` | string \| null | The `id` of the group that draws the module in the `svg` of its face. `null` when the module is not drawn. |
| `connectors` | array | The connectors of the module, as [Connector](#connector), with the `{module}` names resolved for this module. Empty without artwork. A module that is not drawn gives each connector `element_id: null`. |
| `warnings` | array | The [warnings](#warning) of the placement. |

The list has the parents before their children, in the order of the bay keys. A module that the user cannot view, or a module in a module bay that the user cannot view, has no entry, and its bay shows no module. An empty bay has no entry.

### Warning

| Field | Type | Notes |
|---|---|---|
| `code` | string | One of the codes below. |
| `message` | string | The text that the page shows under the face. |

| Code | Condition |
|---|---|
| `size_mismatch` | The width or the height of the turned module face and the bay differ by more than 1 mm. The module still shows. |
| `placement_not_finite` | The bay rect, or the placement that the plugin computes from it, is not a finite number. When the bay rect is finite, the module shows as an outline. Otherwise it is not drawn. |
| `damaged_artwork` | A stored file of the active revision of the module type is missing or does not match its digest. The module shows as an outline. |
| `too_deep` | The module is more than 8 modules deep on its placement path. The module shows as an outline. |
| `duplicate_bay` | Two or more bays of the parent name the module bay of the module after `{module}` resolves. The module shows only in the first bay key. |

---

## Compatibility

A plugin version without this endpoint gives `404` for its URL. A client that has seen the device at `/api/dcim/devices/<id>/` can read that `404` as "not supported".
