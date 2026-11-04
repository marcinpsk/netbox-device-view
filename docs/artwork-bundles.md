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

To check a definition before an import, use the [`device_view_check` command](library-check.md).

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
- `title` and `desc` contain only text.
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

The source that first imports a definition key owns the key. Each Data Source is one source, and all bundle uploads together are one source. An import of a key from another source fails. A Data Source that owns a key with revisions cannot be deleted. To move a key to another source, [transfer the key](#transfer-a-definition-key).

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
- a stored file does not match its recorded size and SHA-256 (the file changed while a sync read it. The import clears the recorded SHA-256 of the file, so the next sync reads it again. Check for updates, then import again.),
- a file that a manifest names is missing.

The plugin lets only one sync of a Data Source run at a time. A second sync that starts during a sync fails, and NetBox sets the source status to **Failed**. Check for updates again after the first sync completes. A sync that ran before you installed the plugin does not count: check for updates once before the first import.

### Permissions

- An import needs the **add** permission on **Definition revision**, and the **view** permission on the Data Source and on every Data File below the prefix.
- **Check for updates** needs the NetBox **sync** permission on the Data Source.

---

## Transfer a definition key

A transfer gives a definition key to another source: a Data Source or the offline uploads. After the transfer, only the new owner can import revisions of the key. An import from the old owner fails. The existing revisions and their provenance do not change.

In the UI, go to **Plugins → Visual Definitions**, open the definition and click **Edit**. Select the new **Key owner**, or clear the field for the offline uploads, and click **Save**.

To find the keys of one source, filter the list by **Key owner**. With the API, use `?key_owner_id=<id>`, or `?key_owner_id=null` for the offline uploads.

With the REST API, send the Data Source ID, or `null` for the offline uploads:

```bash
curl -X PATCH \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"key_owner": 3}' \
  https://netbox.example.com/api/plugins/device_view/artwork-definitions/12/
```

A `PUT` must name `key_owner`. A bulk `PATCH` of the list URL with `[{"id": 12, "key_owner": 3}]` transfers several keys with the same checks. The key and the kind cannot be changed. An import creates a definition, so the API cannot create or delete one. The changelog of the definition records each transfer. An import that runs during a transfer waits for it and then uses the new owner.

### Permissions

- A transfer needs the **change** permission on **Visual definition**. Constraints of that permission apply before and after the change.
- The new owner must be a Data Source that you can view.
- The definition pages and the API need the **view** permission on **Visual definition**.

---

## Activate a revision

An association links a device type to one revision of a device definition, or a module type to one revision of a module definition. That revision is the active artwork of the type. The Device View tab and the device page panel of each device of a device type then show the artwork instead of the layout. Each installed module of a module type shows its artwork in the bay of its parent: see [Module bays](#module-bays).

In the UI, go to **Plugins → Artwork Revisions**, open the revision and click **Activate**. For a device definition, select the **Device type**. For a module definition, select the **Module type**. A revision activates only for a type of its kind. Click **Activate**. A type that already has an active revision changes to the selected one. **Plugins → Artwork Associations** lists the active revision of each type, and you can filter it by device type or module type. Delete an association to show the layout of the type again, or the outline of each module of the type. A revision that an association uses cannot be deleted. The API answers such a delete with 409.

The artwork view shows:

- The front face above the rear face, each at the full width of the view. Each face zooms and scrolls in its own panel.
- A link on each connector that has one binding, when the binding finds a component. The binding uses the component type and the exact name. A connector of the device finds only components of the device itself, not of its modules. A connector of an installed module finds only components of that module. A click on the connector opens the component.
- The installed modules in their bays, with a [warning list](#module-bays) below a face when a placement has a problem.
- The [details](#connector-details) of each connector.
- Marks for the connectivity facts of each connector, and a legend below the faces. Each fact has its own mark, so a disabled interface with a cable shows both. A mark changes only the outline, and each mark has its own pattern, so no fact depends on colour alone.
- A tooltip on each connector that names every fact: enabled or disabled, the cable status or no cable, marked connected, path complete or incomplete, and the peers that you can view. When the connector has its own title, the tooltip shows that title first.

| Mark | Fact |
|---|---|
| Solid outer ring | A cable with status **Connected**. |
| Dashed outer ring | A cable with status **Planned**. |
| Dotted outer ring | A cable with status **Decommissioning**. |
| Long-dashed outer ring | A cable with a status that NetBox configuration adds. |
| Double thin ring | **Mark connected**, with no cable. |
| Dashed inner ring | A disabled interface. |
| Thin ring with a gap | Several components: two or more bindings, and each finds its component. |
| Dashed thin ring with a gap | No component found: at least one binding finds no component. |

A cable that you cannot view gives no cable mark. The path state is in the tooltip only, because a cable mark does not show where the path ends. The [artwork API](artwork-api.md#binding) gives each fact as its own field.

Decorative artwork has no link. A connector whose component does not exist, or that you cannot view, has no link and has the mark for no component found. The plugin never selects one component for a connector with several bindings: a click opens its details instead. When a stored artwork file is missing or does not match its SHA-256, the device shows **Artwork damaged** instead of the artwork. Import the revision again to repair the file.

A virtual chassis shows the artwork of each member when every member that you can view has active artwork. Otherwise it shows the layouts.

### Module bays

A bay of the artwork shows the module that NetBox records in its module bay. A bay of a device definition maps to a module bay of the device by its exact name. A bay of a module definition maps to a module bay of the installed module, and its `{module}` tokens resolve as NetBox names the bays of that module. Thus a transceiver in a bay of a network module shows inside the network module, at any depth. Inventory items and device bays do not show.

The plugin places the module face in the bay at true scale:

1. It moves the face so that its `viewBox` starts at the origin.
2. It turns the face by the `rotate` value of the bay: 0, 90, 180 or 270 degrees, clockwise.
3. It puts the top-left corner of the turned face at the top-left corner of the bay.
4. It clips the module to the bay rect.

The module draws as its own file does: it does not take the presentation attributes, opacity or clip of the groups of the parent artwork. Only `font-family` passes from the parent, because its initial value depends on the browser. Set the font of module text in the module artwork.

Each installed module uses the active revision of its own module type. The revision of the device does not select the revisions of its modules. Two modules of one type in two bays have their own ids, links and connector states.

- A click on the body of a module opens the module. A click on one of its connectors opens the component, or the details, as for a connector of the device. The details name the module.
- A module type without active artwork shows as an outline of the bay with the model of the type. A click on it opens the module. The outline has no bays, so the modules that are installed in that module do not show.
- An empty bay shows only the artwork of the parent.
- A module or a module bay that you cannot view does not show, so its bay looks empty.

A list below the face shows these warnings:

- The width or the height of the turned module face and the bay differ by more than 1 mm. The module still shows. Correct the bay rect or the `viewBox`.
- A value of the bay rect is not a finite number. The module does not show.
- A stored file of the active revision of the module type is missing or does not match its digest. The module shows as an outline. Import the revision again.
- A module is more than 8 modules deep: for example the ninth module in a chain of modules in bays of modules. It shows as an outline, and the modules in it do not show. The limit keeps the page below the nesting depth that browsers parse.
- Two bays of a module definition name one module bay after `{module}` resolves, for example `Slot {module}` and `Slot 1` in a module at position 1. The module shows only in the first bay key, in sort order. The import cannot find this, because the names differ before NetBox resolves them.

The [artwork API](artwork-api.md#module) gives each placed module and its warnings.

### Connector details

The details of a connector show each binding. For a component that the binding finds, the details show the component, its module, enabled or disabled, the cable and its status, marked connected, the path state and the peers, each with a link, and a link to the trace. They show only the objects that you can view: a hidden cable shows as no cable, and a hidden peer is not in the list.

To open the details:

- Click **Details** in the toolbar of the face. While the button is on, a click or a tap on a connector opens its details instead of the component. Click **Details** again to turn it off.
- Focus a connector with the keyboard and press **Space**. **Enter** opens the component, or its details while **Details** is on.
- Click a connector that has no single component. A connector with several components opens a chooser of its components. A connector with a missing binding opens the details of each binding.

Press **Escape** or click the close button to close the details. The focus then goes back to the connector.

### Permissions

- A first activation for a type needs the **add** permission on **Artwork association**. A later activation needs the **change** permission. Constraints of these permissions apply to the association after the change.
- You must be able to view the revision and the type.
- The association pages need the **view** permission on **Artwork association**.
- The artwork view needs no permission on the association or the revision. Components, cables, modules and module bays show only when you can view them.
- An activation fails when a [binding override](#binding-overrides) of the definition key, for the type or for one of its devices or installed modules, names a connector that the revision does not have. The message names each such override. If you cannot view all of them, the device of each, and the installed module of each, the message names none.

---

## Binding overrides

A binding override replaces the bindings of one connector without a change to the NetBox component names or to the artwork. Its scope is one of:

- A device type: the override applies to each device of the type.
- A device: the override applies to that device only.
- A module type: the override applies to each installed module of the type.
- An installed module: the override applies to that module only.

The bindings of a connector resolve in this order: the bindings of the artwork, then the override of the type, then the override of the device or the installed module. Each level replaces the list of the level below it. The lists do not merge. The Device View tab, the device page panel and the [artwork API](artwork-api.md) use the same resolution.

An override has the schema of the `bindings` of a manifest connector: 0 to 16 bindings, each with a `component` type and a `name`. An empty list leaves the connector with no binding. A binding that finds no component is a missing binding: the connector has the mark for no component found.

An override follows the definition key, not a revision. An import of a new revision does not change or delete an override. When you save an override:

- The type of the scope (the device type of a device, the module type of an installed module) must have an active revision. The override gets the definition key of that revision.
- The connector key must be in the active revision. The **Connectors** table of the revision page lists the keys.
- One scope has one override for each connector of a definition key.
- The scope and the connector of an override cannot change. To move an override, add a new one and delete the old one.
- An override of a definition key that is not active for its type does not apply. You cannot change it, but you can delete it.

In the UI, go to **Plugins → Binding Overrides** and click **Add**. Select the scope in one of the tabs, enter the connector key and add a row for each binding. The page of a visual definition lists its overrides. The API endpoint is `/api/plugins/device_view/binding-overrides/`, with the `device_type_id`, `module_type_id`, `device_id`, `module_id`, `definition_id` and `connector_key` filters. The API accepts the same data and checks the same rules as the UI.

### Permissions

- Overrides need the **add**, **change** or **delete** permission on **Binding override**. Constraints of these permissions apply to the override after the change. The pages and the API need the **view** permission.
- To add or change an override, you must be able to view its scope.
- The permission to view a device gives no permission on its overrides.

---

## NetBox Branching

The artwork catalogue is global, as Data Sources are. With [NetBox Branching](https://github.com/netboxlabs/netbox-branching), an import or a key transfer in an active branch changes main at once. A branch merge does not change the catalogue.

An association links to a device type, so it belongs to the branch: an activation in a branch changes only that branch, and a merge applies it to main. A binding override belongs to the branch in the same way.

---

## Storage

The plugin keeps the artwork files in NetBox media storage at `artwork/<xx>/<sha256>.<ext>`. Each file is written once. Identical files share one stored file. Each read checks the SHA-256 of the file. An import writes a missing or damaged file again.

A NetBox system job, **Artwork blob cleanup**, runs daily. It removes stored files that no revision uses and that are older than one day.

!!! note
    NetBox serves media files to all logged-in users. Do not put confidential content in artwork.
