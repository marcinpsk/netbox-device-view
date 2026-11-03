"""The per-type advisory transaction lock of a device type or a module type.

Activation and every writer of a binding override take the lock of the type
that they depend on, so a check of the active artwork and the write that
follows it see no concurrent change.
"""

import hashlib

from dcim.models import DeviceType, ModuleType
from django.db import connections

# The first key of each two-key advisory lock; it keeps these locks apart from others.
_LOCK_CLASSES = {
    model: int.from_bytes(
        hashlib.sha256(
            f"netbox_device_view.{model._meta.label_lower}".encode()
        ).digest()[:4],
        signed=True,
    )
    for model in (DeviceType, ModuleType)
}
# The lock order: device types first, then module types, each by primary key.
_ORDER = (DeviceType, ModuleType)


def _second_key(pk: int) -> int:
    # A primary key above 2**31 wraps, so two types can share a lock.
    return (pk + 2**31) % 2**32 - 2**31


def lock_types(hardware_types, using: str) -> None:
    """Take the lock of each device type or module type until the transaction on ``using`` ends."""
    if not connections[using].in_atomic_block:
        raise RuntimeError("A type lock needs an open transaction.")
    keys = sorted(
        {
            (_ORDER.index(type(hardware_type)), _second_key(hardware_type.pk))
            for hardware_type in hardware_types
        }
    )
    with connections[using].cursor() as cursor:
        for order, key in keys:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(%s, %s)",
                [_LOCK_CLASSES[_ORDER[order]], key],
            )
