"""The per-type advisory transaction lock of a device type or a module type.

Activation and every writer of a binding override take the lock of the type
that they depend on, so a check of the active artwork and the write that
follows it see no concurrent change. A writer of several overrides takes the
bulk lock first.
"""

import hashlib

from dcim.models import DeviceType, ModuleType
from django.db import connections


def _lock_class(name: str) -> int:
    """Return the first key of a two-key advisory lock; it keeps these locks apart from others."""
    return int.from_bytes(
        hashlib.sha256(f"netbox_device_view.{name}".encode()).digest()[:4], signed=True
    )


_LOCK_CLASSES = {
    model: _lock_class(model._meta.label_lower) for model in (DeviceType, ModuleType)
}
_BULK_LOCK = (_lock_class("bindingoverride.bulk"), 0)
# The lock order: device types first, then module types, each by primary key.
_ORDER = (DeviceType, ModuleType)


def _second_key(pk: int) -> int:
    # A primary key above 2**31 wraps, so two types can share a lock.
    return (pk + 2**31) % 2**32 - 2**31


def lock_bulk_writes(using: str) -> None:
    """Take the lock of a writer of several overrides, before any type lock.

    Two such writers could take the type locks in opposite orders. Each other
    writer and an activation take one type lock only.
    """
    if not connections[using].in_atomic_block:
        raise RuntimeError("A bulk lock needs an open transaction.")
    with connections[using].cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", list(_BULK_LOCK))


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
