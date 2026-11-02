# Artwork API

A read-only, versioned JSON endpoint that gives each device of a device or virtual chassis its active artwork, its layout panels, or the reason it has neither. The Device View tab uses the same resolution code, so the API and the page bind the same components in the same state.

The [rendered-layout API](rendered-layout-api.md) does not change. It serves YAML layouts only.

---

## Endpoint

```
GET /api/plugins/device_view/devices/<device_id>/artwork/
```

Access is as for the rendered-layout API: a token, and the **view** permission on the device. Without the permission, the response is `403`. A device outside the permission scope gives `404`, like a device that does not exist.

The artwork needs no permission on the artwork association or the revision. Components, cables, peers and members need the **view** permission.

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
                "connection_state": "connected",
                "cable_color": "ff0000",
                "peer_labels": ["server1 | eth0"],
                "trace_supported": true,
                "occupied": true
              }
            ]
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

Each member resolves alone. A member without artwork or a layout does not hide the others. The Device View tab shows artwork for a virtual chassis only when every member has it; the API gives artwork to each member that has it.

Active artwork comes before a layout. A member gets `"layout"` when its device type has no active artwork and has a YAML `DeviceView` that the user can view. Its `panels` are the [panels of the rendered-layout API](rendered-layout-api.md#panel) for that device alone: the keys are `front` and `rear`, with no member prefix.

### Reasons

| Reason | Condition |
|---|---|
| `no_layout` | No active artwork, and no `DeviceView` that the user can view. |
| `svg_layout_required` | No active artwork, and the `DeviceView` has no YAML layout. |
| `invalid_layout` | No active artwork, and the YAML layout does not parse. |
| `damaged_artwork` | A stored file of the active revision is missing or does not match its digest. Import the revision again to repair it. |

### Artwork

| Field | Type | Notes |
|---|---|---|
| `definition_key` | string | The key of the visual definition, for example `acme/sw-24`. |
| `revision_id`, `digest` | int, string | The active revision. |
| `faces` | array | One entry per face in physical order: `name` (`front`, `rear`, …), `label`, and `svg`. |
| `connectors` | array | One entry per connector of the manifest, sorted by key. |

Each `svg` is the stored face, parsed and written again. It has only the elements of the [SVG profile](artwork-bundles.md#svg-profile): no links, scripts, classes or filters. Every `id` has the prefix `dv-a-d<device_id>-<face>-`, and each reference uses the new id. Thus the faces of several members can share one document. The root keeps its `viewBox` and has no `width` or `height`.

### Connector

| Field | Type | Notes |
|---|---|---|
| `key` | string | The connector key of the manifest. |
| `face` | string | The face that draws the connector. |
| `label` | string \| null | The label of the manifest. |
| `element_id` | string | The `id` of the connector element in the `svg` of its face. Draw state or find a tap target from this element. |
| `bindings` | array | One entry per binding of the manifest, in manifest order. |

The page links a connector to its component only when exactly one binding has `status: "found"`. A client should do the same.

### Binding

| Field | Type | Notes |
|---|---|---|
| `status` | string | `"found"`, or `"missing"` when the device has no component of that type and name that the user can view. A component that the user cannot view is missing. |
| `object_type` | string | `app_label.model_name` of the bound type, for example `"dcim.interface"` or `"dcim.powerport"`. Set for a missing binding too. |
| `object_id` | int \| null | The component. `null` when missing. |
| `name` | string | The component name. For a missing binding, the name in the manifest. |
| `enabled`, `connection_state`, `cable_color`, `peer_labels`, `trace_supported` | | As in a [rendered-layout hotspot](rendered-layout-api.md#hotspot). A missing binding has `null`, `null`, `null`, `[]` and `false`. |
| `occupied` | bool | `true` when the component has a cable that the user can view. The page outlines such a connector. |

---

## Compatibility

A plugin version without this endpoint gives `404` for its URL. A client that has seen the device at `/api/dcim/devices/<id>/` can read that `404` as "not supported".
