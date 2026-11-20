"""A chassis with module bays, the artwork of its modules, and the NetBox inventory that installs them."""

import io

from dcim.models import (
    Device,
    DeviceRole,
    DeviceType,
    Interface,
    InterfaceTemplate,
    Manufacturer,
    Module,
    ModuleBay,
    ModuleBayTemplate,
    ModuleType,
    PowerPortTemplate,
    Site,
)

from netbox_device_view.artwork.importing import import_bundle

from .activation import activate as activate_pairs
from .artwork_samples import bundle, definition


def manifest(key, kind, faces, connectors="", bays=""):
    return f"""\
schema: 1
key: {key}
kind: {kind}
title: {key}
hardware:
  manufacturer: ACME
  sku: {key.split("/")[1].upper()}
  references:
    - {{title: Guide, edition: "1", url: "https://example.com/guide.pdf"}}
license: MIT
authors: [Test Author]
faces:
{faces}connectors:
{connectors or "  {}"}
bays:
{bays or "  {}"}
"""


CHASSIS_MANIFEST = manifest(
    "acme/ch-4",
    "device",
    "  front: {file: front.svg}\n",
    "  con-eth1: {face: front, type: rj45, bindings: [{component: interface, name: eth1}]}",
    """\
  bay-nm1: {face: front, module_bay: NM 1}
  bay-nm2: {face: front, module_bay: NM 2}
  bay-psu: {face: front, module_bay: PSU, rotate: 90}
  bay-fan: {face: front, module_bay: FAN}
  bay-spare: {face: front, module_bay: Spare}""",
)
CHASSIS_SVG = """\
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 440 44">
  <rect width="440" height="44" fill="#333"/>
  <rect id="con-eth1" x="10" y="10" width="12" height="10" fill="#111"/>
  <rect id="bay-nm1" x="200" y="7" width="100" height="30" fill="#111"/>
  <rect id="bay-nm2" x="320" y="7" width="100" height="30" fill="#111"/>
  <g><rect id="bay-psu" x="120" y="2" width="30" height="40" fill="#111"/></g>
  <rect id="bay-fan" x="40" y="4" width="20" height="36" fill="#111"/>
  <rect id="bay-spare" x="70" y="4" width="20" height="36" fill="#111"/>
</svg>
"""
NM_MANIFEST = manifest(
    "acme/nm-2x",
    "module",
    "  installed: {file: installed.svg}\n",
    """\
  con-p1:
    face: installed
    type: sfp-plus
    label: P1
    bindings: [{component: interface, name: "TenGigabitEthernet1/{module}/1"}]""",
    '  bay-slot: {face: installed, module_bay: "Slot {module}"}',
)
NM_SVG = """\
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 30">
  <defs><linearGradient id="metal"><stop offset="0" stop-color="#555"/></linearGradient></defs>
  <rect width="100" height="30" fill="url(#metal)"/>
  <rect id="con-p1" x="10" y="10" width="14" height="10" fill="#111"/>
  <rect id="bay-slot" x="40" y="10" width="14" height="10" fill="#000"/>
</svg>
"""
SFP_MANIFEST = manifest(
    "acme/sfp-10g",
    "module",
    "  installed: {file: installed.svg}\n",
    '  con-port: {face: installed, type: lc, bindings: [{component: interface, name: "Port {module}"}]}',
)
SFP_SVG = """\
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 14 10">
  <rect width="14" height="10" fill="#999"/>
  <rect id="con-port" x="2" y="2" width="10" height="6" fill="#222"/>
</svg>
"""
PSU_MANIFEST = manifest(
    "acme/psu-1",
    "module",
    "  installed: {file: installed.svg}\n",
    "  con-inlet: {face: installed, type: c14, bindings: [{component: powerport, name: Inlet}]}",
)
PSU_SVG = """\
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 30">
  <rect width="40" height="30" fill="#777"/>
  <rect id="con-inlet" x="5" y="5" width="10" height="8" fill="#111"/>
</svg>
"""
DEFINITIONS = {
    "ACME/CH-4": {"manifest.yaml": CHASSIS_MANIFEST, "front.svg": CHASSIS_SVG},
    "ACME/NM-2X": {"manifest.yaml": NM_MANIFEST, "installed.svg": NM_SVG},
    "ACME/SFP-10G": {"manifest.yaml": SFP_MANIFEST, "installed.svg": SFP_SVG},
    "ACME/PSU-1": {"manifest.yaml": PSU_MANIFEST, "installed.svg": PSU_SVG},
}


class Inventory:
    """Create the chassis inventory: two network modules of one type, a transceiver in the first, a PSU, a fan without artwork and an empty bay."""

    def __init__(self, admin, name="ch1", activate=True):
        manufacturer = Manufacturer.objects.get_or_create(name="ACME", slug="acme")[0]
        self.device_type = DeviceType.objects.get_or_create(
            manufacturer=manufacturer, model="CH-4", slug="ch-4"
        )[0]
        self.device = Device.objects.create(
            name=name,
            device_type=self.device_type,
            site=Site.objects.get_or_create(name="Site", slug="site")[0],
            role=DeviceRole.objects.get_or_create(name="Role", slug="role")[0],
        )
        self.eth1 = Interface.objects.create(
            device=self.device, name="eth1", type="1000base-t"
        )
        self.nm_type = self.module_type(manufacturer, "NM-2X")
        InterfaceTemplate.objects.get_or_create(
            module_type=self.nm_type,
            name="TenGigabitEthernet1/{module}/1",
            type="10gbase-x-sfpp",
        )
        ModuleBayTemplate.objects.get_or_create(
            module_type=self.nm_type, name="Slot {module}", position="7"
        )
        self.sfp_type = self.module_type(manufacturer, "SFP-10G")
        InterfaceTemplate.objects.get_or_create(
            module_type=self.sfp_type, name="Port {module}", type="10gbase-x-sfpp"
        )
        self.psu_type = self.module_type(manufacturer, "PSU-1")
        PowerPortTemplate.objects.get_or_create(module_type=self.psu_type, name="Inlet")
        self.fan_type = self.module_type(manufacturer, "FAN-1")
        bays = {
            bay_name: ModuleBay.objects.create(
                device=self.device, name=bay_name, position=position
            )
            for bay_name, position in (
                ("NM 1", "1"),
                ("NM 2", "2"),
                ("PSU", "1"),
                ("FAN", "1"),
                ("Spare", "3"),
            )
        }
        self.nm1 = self.install(bays["NM 1"], self.nm_type)
        self.nm2 = self.install(bays["NM 2"], self.nm_type)
        self.sfp = self.install(
            ModuleBay.objects.get(module=self.nm1, name="Slot 1"), self.sfp_type
        )
        self.psu = self.install(bays["PSU"], self.psu_type)
        self.fan = self.install(bays["FAN"], self.fan_type)
        if activate:
            self.revisions = import_definitions(admin)
            activate_pairs(
                admin,
                *(
                    (self.revisions[key], hardware_type)
                    for key, hardware_type in (
                        ("acme/ch-4", self.device_type),
                        ("acme/nm-2x", self.nm_type),
                        ("acme/sfp-10g", self.sfp_type),
                        ("acme/psu-1", self.psu_type),
                    )
                ),
            )

    @staticmethod
    def module_type(manufacturer, model):
        return ModuleType.objects.get_or_create(manufacturer=manufacturer, model=model)[
            0
        ]

    def install(self, bay, module_type):
        return Module.objects.create(
            device=self.device, module_bay=bay, module_type=module_type
        )

    def interface(self, module, name):
        return Interface.objects.get(device=self.device, module=module, name=name)


def import_definitions(admin):
    """Import the four definitions in one bundle; return {key: revision}."""
    entries = {}
    for directory, files in DEFINITIONS.items():
        entries.update(definition(directory, files))
    return {
        imported.revision.definition.key: imported.revision
        for imported in import_bundle(admin, "modules.zip", io.BytesIO(bundle(entries)))
    }
