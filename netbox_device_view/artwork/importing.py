"""Import validated definitions as candidate revisions; the UI and the API both call this."""

from dataclasses import dataclass
from posixpath import splitext

from django.core.exceptions import PermissionDenied
from django.db import transaction

from ..models import (
    DefinitionRevision,
    RevisionFile,
    RevisionImport,
    VisualDefinition,
)
from .bundle import MANIFEST_NAME, Definition, read_bundle
from .errors import ArtworkError, Issue
from .storage import lock_digest, store_blob

PERMISSION = "netbox_device_view.add_definitionrevision"
MAX_NAME_LENGTH = 255


@dataclass(frozen=True)
class ImportedRevision:
    revision: DefinitionRevision
    created: bool


def can_import(user) -> bool:
    return user.has_perm(PERMISSION)


def _definition(item: Definition) -> VisualDefinition:
    kind = item.manifest["kind"]
    visual, _ = VisualDefinition.objects.get_or_create(
        key=item.manifest["key"], defaults={"kind": kind}
    )
    if visual.kind != kind:
        path = f"{item.directory}/{MANIFEST_NAME}" if item.directory else MANIFEST_NAME
        raise ArtworkError(
            [Issue(path, None, f"The key {visual.key} is a {visual.kind} definition.")]
        )
    return visual


def _import(user, bundle_name: str, item: Definition) -> ImportedRevision:
    revision, created = DefinitionRevision.objects.get_or_create(
        digest=item.digest,
        defaults={"definition": _definition(item), "manifest": item.manifest},
    )
    # Each import writes a missing or damaged blob again, also for a known revision.
    blobs = {
        path: store_blob(data, splitext(path)[1].lstrip("."))
        for path, data in sorted(item.files.items())
    }
    if created:
        RevisionFile.objects.bulk_create(
            RevisionFile(revision=revision, path=path, blob=blob)
            for path, blob in blobs.items()
        )
    RevisionImport.objects.create(
        revision=revision,
        user=user,
        user_name=user.get_username(),
        bundle_name=bundle_name[:MAX_NAME_LENGTH],
        path=item.directory,
    )
    return ImportedRevision(revision, created)


def import_bundle(user, bundle_name: str, fileobj) -> list[ImportedRevision]:
    """Validate the whole bundle, then import every definition in one transaction."""
    if not can_import(user):
        raise PermissionDenied
    definitions = sorted(read_bundle(fileobj), key=lambda item: item.manifest["key"])
    with transaction.atomic():
        # One lock order for all imports: every digest, sorted, before any write.
        for digest in sorted(
            {d for item in definitions for d in item.file_digests.values()}
        ):
            lock_digest(digest)
        results = [_import(user, bundle_name, item) for item in definitions]
        pks = {result.revision.pk for result in results}
        permitted = DefinitionRevision.objects.restrict(user, "add").filter(pk__in=pks)
        if permitted.count() != len(pks):
            raise PermissionDenied
    return results
