# Artwork Bundles

An artwork bundle is a `.zip` file with one or more visual definitions. Each visual definition is reusable SVG artwork of one NetBox hardware type (a device type or a module type) and a manifest that names its connectors and module bays.

An import validates the whole bundle and stores each definition as a candidate revision. An import does not change a device view. Activation of a revision is a separate step.

---

## Bundle layout

A bundle has the layout of an artwork repository. Each definition is one directory with a `manifest.yaml` file and one SVG file for each physical face:

```text
artwork/
├── ACME/
│   ├── SW-24/
│   │   ├── manifest.yaml
│   │   ├── front.svg
│   │   └── rear.svg
│   └── NM-2X/
│       ├── manifest.yaml
│       └── installed.svg
```

- A definition directory contains only `manifest.yaml` and the face files that the manifest names. Other files fail the import. Two faces cannot name the same file.
- Every file must be inside a definition directory. A definition directory cannot be inside another one.
- A definition key can occur in only one directory of a bundle.
- Archive members are stored or deflated, and not encrypted. The manifest and the SVG files are UTF-8.
- The archive uses the classic zip format. A zip64 archive fails. A bundle fits the classic format, because of its size and member limits.
- If one definition fails, the import stores nothing.

---

## Manifest

The manifest is YAML with `schema: 1`. The JSON Schema is [`netbox_device_view/artwork/manifest.schema.json`](https://github.com/marcinpsk/netbox-device-view/blob/develop/netbox_device_view/artwork/manifest.schema.json). Every mapping rejects unknown keys.

```yaml
schema: 1
key: acme/sw-24                 # the definition key: <vendor>/<part>, lower case
kind: device                    # device | module
title: ACME SW-24
hardware:
  manufacturer: ACME
  sku: SW-24-AC                 # the exact hardware SKU
  references:                   # 1 to 8 hardware documents
    - title: ACME SW-24 Hardware Guide
      edition: Rev B
      figure: "1.2"             # optional
      url: https://example.com/sw-24.pdf
license: MIT                    # an SPDX licence expression
authors: [Jane Doe]
faces:                          # device: front and/or rear; module: installed
  front: {file: front.svg}
  rear: {file: rear.svg}
connectors:                     # optional; keys start with con-
  con-eth1:
    face: front
    type: rj45
    label: "1"                  # optional
    bindings:                   # 0 to 16 NetBox components
      - {component: interface, name: eth1}
bays:                           # optional; keys start with bay-
  bay-nm:
    face: front
    module_bay: Network Module
    rotate: 0                   # 0 | 90 | 180 | 270, default 0
```

Rules:

- `component` is one of `interface`, `consoleport`, `consoleserverport`, `powerport`, `poweroutlet`, `frontport`, `rearport`.
- A device definition binds components by their exact name. A module definition writes names as NetBox template names, for example `TenGigabitEthernet1/{module}/1`. Only a module definition can use `{module}`.
- Two bays cannot map to the same `module_bay`.
- The licence is checked with the `license-expression` library. A licence reference (`LicenseRef-...`) is not accepted.
- URLs use `https` and have no user name or password.
- No string can contain control characters or format characters.
- YAML anchors, aliases, explicit tags (for example `!!str`), duplicate keys and values other than strings, integers, booleans, lists and mappings fail the import.

---

## SVG profile

Each face is one SVG file. The `viewBox` is in millimetres of the real face. The import rejects SVG content that is not in the profile. It never removes or repairs content.

- Elements: `svg` (root only), `g`, `defs`, `symbol`, `use`, `path`, `rect`, `circle`, `ellipse`, `line`, `polyline`, `polygon`, `text`, `tspan`, `title`, `desc`, `linearGradient`, `radialGradient`, `stop`, `clipPath`.
- Not allowed: scripts, `style` elements, `image`, links, masks, filters, patterns, markers, `foreignObject`, animation, event attributes, `class`, a DOCTYPE, entities and processing instructions. Only the SVG and XLink namespaces are allowed, so export plain SVG from your editor.
- Styling: presentation attributes and `style` attributes with presentation properties only. A paint `url()` can name only a gradient in the same file. A `clip-path` can name only a `clipPath` in the same file.
- References: `href` and `xlink:href` are `#id` references in the same file. Reference cycles fail.
- A connector is one element whose `id` is its connector key. A bay is one `rect` whose `id` is its bay key, with unitless `x`, `y`, `width` and `height`. A bay and its ancestor groups have no `transform`.
- The connector and bay keys of the manifest and the `con-` and `bay-` ids of each face must be equal.

---

## Limits

The import stops at these limits before it does more work:

| Limit | Value |
| --- | --- |
| Bundle file | 50 MiB compressed |
| Bundle content | 200 MiB expanded |
| Compression ratio | 100:1 per member |
| Definitions | 500 per bundle |
| Files | 64 per definition |
| Manifest | 256 KiB |
| SVG file | 1 MiB |
| SVG elements | 20000 per file |
| SVG nesting | 32 levels |
| `use` expansion | 50000 nodes, 8 levels |
| `viewBox` size | 1 to 5000 mm |
| Data Source files | 32000 and 200 MiB below the prefix |

Each error names the file and, where one applies, the line. The import shows at most 50 errors.

---

## Revision identity

A revision is identified by its digest: the SHA-256 of the lines `<path>\0<file SHA-256>\n`, sorted by path. The paths are relative to the definition directory. Thus the same files give the same revision in every directory and every bundle. An import of known content adds an import record to the existing revision and stores no new revision.

Each import record keeps the provenance: the time, the user, the bundle file name or the Data Source name, and the directory. For a Data Source it also keeps the time of the last sync. NetBox does not record the Git commit of a sync, so the provenance shows no commit, and the digest identifies the content. A stored revision cannot be changed. You can delete it.

The source that first imports a definition key owns the key. Each Data Source is one source, and all bundle uploads together are one source. An import of a key from another source fails. A Data Source that owns a key with revisions cannot be deleted.

---

## Import a bundle

In the UI, go to **Plugins → Artwork Revisions** and click **Import bundle**. Then select the `.zip` file and click **Import**.

With the REST API, send the file as the multipart field `bundle`:

```bash
curl -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -F "bundle=@artwork.zip" \
  https://netbox.example.com/api/plugins/device_view/artwork-bundles/
```

The response is `201` with the revisions and a `created` flag for each one, or `200` when all revisions were known. For a known revision that you cannot view, the response shows only its key and digest. A bundle that fails gives `400` with `errors`, each with `path`, `line` and `message`. The UI and the API use the same validation and import code.

`GET /api/plugins/device_view/artwork-revisions/` lists the revisions with their manifest, files and import records.

### Permissions

- An import needs the **add** permission on **Definition revision**. Constraints of that permission apply to the imported revisions.
- The revision pages and the API need the **view** permission. A delete needs the **delete** permission.
- The permission to view a device does not give any of these permissions.

---

## Import from a Data Source

A NetBox Data Source, for example a Git repository, can supply the same files as a bundle. A sync only fetches the files into NetBox. It does not change a revision. An import reads the synchronized files and stores each definition as a candidate revision, with the same validation as a bundle. The same files give the same revision as a bundle.

In the UI, go to **Plugins → Artwork Revisions** and click **Import from Data Source**. Select the Data Source and the directory prefix. Every file below the prefix must be in a definition directory, so select the directory that holds the definitions. Click **Check for updates** to start the NetBox sync of the source, then click **Import** when the sync has completed.

With the REST API, send the Data Source ID and the prefix:

```bash
curl -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"data_source": 3, "prefix": "artwork/"}' \
  https://netbox.example.com/api/plugins/device_view/artwork-source-imports/
```

The response is the same as for a bundle.

The import reads all files below the prefix in one database query. It fails when:

- the Data Source status is not **Completed**,
- a sync of the source has not finished,
- a stored file does not match its recorded SHA-256,
- a file that a manifest names is missing.

The plugin lets only one sync of a Data Source run at a time. A second sync that starts during a sync fails, and NetBox sets the source status to **Failed**. Check for updates again after the first sync completes. A sync that ran before you installed the plugin does not count: check for updates once before the first import.

### Permissions

- An import needs the **add** permission on **Definition revision**, and the **view** permission on the Data Source and on every Data File below the prefix.
- **Check for updates** needs the NetBox **sync** permission on the Data Source.

---

## Storage

The plugin keeps the artwork files in NetBox media storage at `artwork/<xx>/<sha256>.<ext>`. Each file is written once. Identical files share one stored file. Each read checks the SHA-256 of the file. An import writes a missing or damaged file again.

A NetBox system job, **Artwork blob cleanup**, runs daily. It removes stored files that no revision uses and that are older than one day.

!!! note
    NetBox serves media files to all logged-in users. Do not put confidential content in artwork.
