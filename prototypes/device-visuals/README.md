# Device artwork interaction prototype

Open `index.html` in a modern browser. It is self-contained and works without a server. All state is in memory. Reload to reset it.

For a local preview, run from the repository root:

```sh
python3 -m http.server 8768 --bind 127.0.0.1 --directory prototypes/device-visuals
```

Open `http://127.0.0.1:8768/`. This address is local to the machine running the server.

## Question

Can detailed artwork, nested installed parts, and per-part schematic fallback remain understandable while retaining existing direct navigation and a separate update-review workflow?

## Compare the views

Use the bottom switcher or the left/right arrow keys. The URL preserves the chosen view.

- `?variant=A`: Conventional device view with one physical face at a time.
- `?variant=B`: Front and rear drawings alongside a component list.
- `?variant=C`: Assembly navigation with a focused network-module drawing.

All three views use the same sample data and update workflow.

## Try these scenarios

1. Open the ACS rear panel. Click `ttyS1` to visit its simulated NetBox record. Switch to the front to see the USB and decorative LEDs.
2. Select the cabled-and-disabled scenario. Inspect ACS `eth0` or Cisco `GigabitEthernet1/0/1`. Cable attachment stays recorded when administrative state is disabled.
3. Select Cisco and the missing-artwork scenario. The installed network module and optics use a schematic while the chassis keeps its artwork.
4. Select the missing-transceiver scenario. The first optic bay has no installed module record. It does not claim the physical slot is empty.
5. Select missing/ambiguous bindings. The first connector opens unresolved details; the second opens a candidate chooser.
6. Select repeated artwork. The two devices have independent component keys and record targets.
7. Select the virtual-chassis scenario. The second member uses schematic rendering and retains its member label.
8. In view C, select the network module to inspect the optic bodies and connectors separately.
9. Open the artwork-update link. It opens the plugin review page, without adding a device tab.
10. Check and fetch a configured source, or load the offline bundle example. The active revision stays unchanged until activation.
11. Enable a broken binding or restricted review visibility. Activation is blocked. Correct the sample binding or restore visibility to continue.
12. Activate revision 2. The instance-specific revision remains pinned. Restore the previous revision to explore rollback.

## Browser observations

- Exercised direct connector navigation, explicit details, keyboard Enter, and ambiguous-target selection.
- Confirmed distinct ACS repeated-instance record targets after correcting a prototype fixture collision.
- Exercised all three views across six Cisco scenarios. No duplicate DOM IDs or JavaScript exceptions were observed.
- Checked all three views at a 390-pixel viewport. The page fits; hardware drawings scroll inside their panels.
- Exercised configured-source selection, offline import, rejected import, activation blockers, activation, and rollback.
- Imported candidates do not change the active revision. Instance selections remain pinned.

These are browser walkthroughs of sample behavior, not production tests. No NetBox suite was run.

## Limits

The drawings are original, simplified SVG studies. They need human review against exact hardware before becoming library artwork. The ACS placement follows the manufacturer's front/rear diagram. Cisco drawings explore composition and do not claim manufacturing accuracy.

References:

- [Vertiv ACS6000 installer guide, figure 2.2](https://www.vertiv.com/4af99d/globalassets/products/monitoring-control-and-management/serial-consoles-and-gateways/avocent-acs-6000-advanced-console-server-installeruser-guide.pdf)
- [Cisco Catalyst 9300 hardware overview](https://www.cisco.com/c/en/us/td/docs/switches/lan/catalyst9300/hardware/install/b_c9300_hig/Product-overview.html)

Repository checks, imports, validation, permissions, object navigation, and rollback are simulated. No remote requests or file uploads occur. The prototype does not validate SVG safety, API compatibility, database concurrency, or NetBox permissions. Those remain specification and integration requirements.

The nested Cisco example deliberately models transceivers as modules in bays. It does not claim that the current community template provides those bays unchanged.

## Status

Evaluated by the user on 2026-10-22. The preferred direction combines view B's
simultaneous front/rear drawings, without its "Recorded components" list, with
view C's assembly navigation and focused subcomponent view. The prototype still
contains the original three alternatives; this combined view is not implemented.

The first milestone imports existing YAML/CSS layouts from native Git-backed
NetBox Data Sources: [Git import spec, issue 2](https://github.com/marcinpsk/netbox-device-view/issues/2).
This branch archives the artwork exploration for the
[SVG and UI follow-up spec, issue 3](https://github.com/marcinpsk/netbox-device-view/issues/3).
It adds no plugin runtime behavior.

Only this README and the self-contained HTML are published. Reference PDFs,
local interview notes, and private workflow files are not part of this branch.
