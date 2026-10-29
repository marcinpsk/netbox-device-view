"""Small valid artwork definitions and bundles for the artwork tests."""

import io
import zipfile

DEVICE_MANIFEST = """\
schema: 1
key: acme/sw-24
kind: device
title: ACME SW-24
hardware:
  manufacturer: ACME
  sku: SW-24-AC
  references:
    - title: ACME SW-24 Hardware Guide
      edition: Rev B
      figure: "1.2"
      url: https://example.com/sw-24.pdf
license: MIT
authors: [Test Author]
faces:
  front: {file: front.svg}
  rear: {file: rear.svg}
connectors:
  con-eth1:
    face: front
    type: rj45
    label: "1"
    bindings:
      - {component: interface, name: eth1}
  con-psu:
    face: rear
    type: c14
    bindings:
      - {component: powerport, name: PSU1}
bays:
  bay-nm:
    face: front
    module_bay: Network Module
"""

FRONT_SVG = """\
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 440 44">
  <rect x="0" y="0" width="440" height="44" fill="#333"/>
  <rect id="con-eth1" x="10" y="10" width="12" height="10" fill="#111"/>
  <rect id="bay-nm" x="300" y="5" width="100" height="30" fill="none" stroke="#999"/>
</svg>
"""

REAR_SVG = """\
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 440 44">
  <rect x="0" y="0" width="440" height="44" fill="#333"/>
  <rect id="con-psu" x="400" y="10" width="30" height="20" fill="#111"/>
</svg>
"""

MODULE_MANIFEST = """\
schema: 1
key: acme/nm-2x
kind: module
title: ACME NM-2X
hardware:
  manufacturer: ACME
  sku: NM-2X
  references:
    - {title: ACME NM-2X Guide, edition: "1", url: "https://example.com/nm.pdf"}
license: MIT OR Apache-2.0
authors: [Test Author]
faces:
  installed: {file: installed.svg}
connectors:
  con-p1:
    face: installed
    type: sfp-plus
    bindings:
      - {component: interface, name: "TenGigabitEthernet1/{module}/1"}
"""

MODULE_SVG = """\
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 30">
  <rect x="0" y="0" width="100" height="30" fill="#444"/>
  <rect id="con-p1" x="10" y="10" width="14" height="10" fill="#111"/>
</svg>
"""

DEVICE_FILES = {
    "manifest.yaml": DEVICE_MANIFEST,
    "front.svg": FRONT_SVG,
    "rear.svg": REAR_SVG,
}
MODULE_FILES = {"manifest.yaml": MODULE_MANIFEST, "installed.svg": MODULE_SVG}


def definition(directory, files):
    """Return the bundle entries of one definition directory."""
    return {f"{directory}/{name}": content for name, content in files.items()}


def bundle(entries=None, compression=zipfile.ZIP_DEFLATED):
    """Return the bytes of a zip file with the given {path: str or bytes} entries."""
    if entries is None:
        entries = definition("artwork/ACME/SW-24", DEVICE_FILES)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression) as archive:
        for name, content in entries.items():
            if isinstance(content, zipfile.ZipInfo):
                archive.writestr(content, b"")
            else:
                archive.writestr(
                    name, content.encode() if isinstance(content, str) else content
                )
    return buffer.getvalue()
