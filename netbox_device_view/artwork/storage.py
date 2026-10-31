"""Content-addressed artwork blobs in media storage, written once and verified on read."""

import hashlib
import re
from datetime import timedelta

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import connection, transaction
from django.utils import timezone

from ..models import ArtworkBlob, RevisionFile

ROOT = ArtworkBlob.ROOT
ORPHAN_AGE = timedelta(days=1)
_NAME = re.compile(r"(?P<digest>[0-9a-f]{64})\.(?P<extension>[a-z]{1,8})")
# The first key of each two-key advisory lock; it keeps these locks apart from others.
_LOCK_CLASS = int.from_bytes(
    hashlib.sha256(b"netbox_device_view.artwork_blob").digest()[:4], signed=True
)


class DamagedBlob(Exception):
    """The stored bytes of a blob are missing or do not match its digest."""


def lock_digest(digest: str):
    """Take the advisory transaction lock that import and cleanup share for a digest."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(%s, %s)",
            [_LOCK_CLASS, int.from_bytes(bytes.fromhex(digest[:8]), signed=True)],
        )


def _stored_digest(name: str) -> str | None:
    if not default_storage.exists(name):
        return None
    with default_storage.open(name, "rb") as stored:
        return hashlib.sha256(stored.read()).hexdigest()


def store_blob(data: bytes, extension: str) -> ArtworkBlob:
    """Return the blob of the data; write the file when it is absent or damaged.

    Call inside the import transaction, which keeps the digest lock until it commits.
    """
    digest = hashlib.sha256(data).hexdigest()
    lock_digest(digest)
    blob, _ = ArtworkBlob.objects.get_or_create(
        digest=digest, defaults={"size": len(data), "extension": extension}
    )
    if _stored_digest(blob.name) != digest:
        if default_storage.exists(blob.name):
            default_storage.delete(blob.name)
        saved = default_storage.save(blob.name, ContentFile(data))
        if saved != blob.name:
            raise RuntimeError(f"The storage saved {blob.name} as {saved}.")
    return blob


def read_blob(blob: ArtworkBlob) -> bytes:
    """Return the stored bytes of the blob after a digest check."""
    try:
        with default_storage.open(blob.name, "rb") as stored:
            data = stored.read()
    except FileNotFoundError as exc:
        raise DamagedBlob(f"{blob.name} is missing.") from exc
    if hashlib.sha256(data).hexdigest() != blob.digest:
        raise DamagedBlob(f"{blob.name} does not match its digest.")
    return data


def _stored_names():
    """Yield (name, digest, extension) of each blob file under the artwork root."""
    # Object stores have no directories, so exists(ROOT) can be False; list instead.
    try:
        directories, _ = default_storage.listdir(ROOT)
    except FileNotFoundError:
        return
    for directory in directories:
        _, files = default_storage.listdir(f"{ROOT}/{directory}")
        for file in files:
            match = _NAME.fullmatch(file)
            if match and match["digest"][:2] == directory:
                yield f"{ROOT}/{directory}/{file}", match["digest"], match["extension"]


def _is_referenced(digest: str) -> bool:
    return RevisionFile.objects.filter(blob_id=digest).exists()


def cleanup(now=None) -> int:
    """Remove blobs that no revision uses and that are older than ORPHAN_AGE; return the count."""
    cutoff = (now or timezone.now()) - ORPHAN_AGE
    removed = 0
    orphans = ArtworkBlob.objects.filter(created__lt=cutoff).exclude(
        digest__in=RevisionFile.objects.values("blob_id")
    )
    for digest in orphans.values_list("digest", flat=True):
        with transaction.atomic():
            lock_digest(digest)
            blob = ArtworkBlob.objects.filter(digest=digest).first()
            if blob is None or _is_referenced(digest):
                continue
            if default_storage.exists(blob.name):
                default_storage.delete(blob.name)
            blob.delete()
            removed += 1
    for name, digest, extension in list(_stored_names()):
        if default_storage.get_modified_time(name) >= cutoff:
            continue
        with transaction.atomic():
            lock_digest(digest)
            if ArtworkBlob.objects.filter(digest=digest, extension=extension).exists():
                continue
            default_storage.delete(name)
            removed += 1
    return removed
