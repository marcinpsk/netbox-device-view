"""The advisory transaction locks that activation and the binding override writers share."""

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


def _second_key(pk: int) -> int:
    # A primary key above 2**31 wraps, so two types can share a lock.
    return (pk + 2**31) % 2**32 - 2**31


def _lock(keys, using: str) -> None:
    if not connections[using].in_atomic_block:
        raise RuntimeError("An advisory transaction lock needs an open transaction.")
    with connections[using].cursor() as cursor:
        for key in keys:
            cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", list(key))


def lock_bulk_writes(using: str) -> None:
    """Take the lock of a writer of several overrides, before any type lock."""
    _lock([_BULK_LOCK], using)


def lock_types(hardware_types, using: str) -> None:
    """Take the lock of each device type or module type, in a fixed order, until the transaction ends."""
    _lock(
        sorted(
            {
                (_LOCK_CLASSES[type(hardware_type)], _second_key(hardware_type.pk))
                for hardware_type in hardware_types
            }
        ),
        using,
    )
