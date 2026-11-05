"""Copies of the library reference definitions, and NetBox types made from devicetype-library files."""

import io
import zipfile
from pathlib import Path

import yaml
from dcim.models import (
    ConsolePortTemplate,
    ConsoleServerPortTemplate,
    DeviceType,
    InterfaceTemplate,
    Manufacturer,
    ModuleBayTemplate,
    ModuleType,
    PowerPortTemplate,
)

FIXTURES = Path(__file__).parent / "fixtures"
# A copy of the artwork in netbox-device-view-library.
LIBRARY = FIXTURES / "library"
ARTWORK = LIBRARY / "artwork"
# marcinpsk/devicetype-library 7ac9dae, the commit that the library check pins.
COMMUNITY = FIXTURES / "devicetype-library"
# netbox-community/devicetype-library 3c8fdb2, where a type differs from the pinned commit.
UPSTREAM = FIXTURES / "devicetype-library-upstream"
TEMPLATES = {
    "interfaces": InterfaceTemplate,
    "console-ports": ConsolePortTemplate,
    "console-server-ports": ConsoleServerPortTemplate,
    "power-ports": PowerPortTemplate,
}


def definition_bundle(*directories: str) -> bytes:
    """Return a bundle of the library definitions, for example ``Avocent/ACS6048``."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for directory in directories:
            for file in sorted((ARTWORK / directory).iterdir()):
                archive.writestr(f"artwork/{directory}/{file.name}", file.read_bytes())
    return buffer.getvalue()


def community_type(file: Path) -> DeviceType | ModuleType:
    """Create the device type or module type of a devicetype-library file, with its templates."""
    data = yaml.safe_load(file.read_text())
    manufacturer = Manufacturer.objects.get_or_create(
        name=data["manufacturer"], slug=data["manufacturer"].lower()
    )[0]
    kind = file.parent.parent.name
    if kind == "device-types":
        hardware_type = DeviceType.objects.create(
            manufacturer=manufacturer,
            model=data["model"],
            slug=data["slug"],
            part_number=data["part_number"],
            u_height=data["u_height"],
        )
        owner = {"device_type": hardware_type}
    elif kind == "module-types":
        hardware_type = ModuleType.objects.create(
            manufacturer=manufacturer,
            model=data["model"],
            part_number=data["part_number"],
        )
        owner = {"module_type": hardware_type}
    else:
        raise ValueError(f"{file} is not in device-types/ or module-types/.")
    for key, template in TEMPLATES.items():
        template.objects.bulk_create(
            template(**owner, name=item["name"], type=item["type"])
            for item in data.get(key, [])
        )
    ModuleBayTemplate.objects.bulk_create(
        ModuleBayTemplate(
            **owner,
            name=item["name"],
            label=item.get("label", ""),
            position=item["position"],
        )
        for item in data.get("module-bays", [])
    )
    return hardware_type
