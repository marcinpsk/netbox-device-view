"""Database expressions over the stored bytes of a Data File, so that no content reaches Python."""

from django.db.models import Case, CharField, Func, Value, When
from django.db.models.functions import Length
from django.db.models.lookups import LessThanOrEqual


def stored_size():
    return Length("data")


def stored_sha256(max_bytes: int):
    """Return the hex SHA-256 of the stored data; above ``max_bytes`` return NULL and hash nothing."""
    digest = Func(
        Func("data", function="sha256"),
        Value("hex"),
        function="encode",
        output_field=CharField(),
    )
    return Case(When(LessThanOrEqual(stored_size(), max_bytes), then=digest))
