"""Import validated definitions as candidate revisions; the UI and the API both call this."""

from dataclasses import dataclass
from datetime import datetime
from posixpath import splitext

from core.models import DataSource
from django.core.exceptions import PermissionDenied
from django.db import transaction

from ..models import (
    DefinitionRevision,
    RevisionFile,
    RevisionImport,
    VisualDefinition,
)
from .bundle import Definition, read_bundle, read_definitions
from .capture import read_source_files
from .errors import ArtworkError, Issue
from .storage import lock_digest, store_blob

PERMISSION = "netbox_device_view.add_definitionrevision"


@dataclass(frozen=True)
class ImportedRevision:
    revision: DefinitionRevision
    created: bool


@dataclass(frozen=True)
class Provenance:
    """Where the definitions came from: a bundle file, or a Data Source and its last sync."""

    source_name: str
    data_source: DataSource | None = None
    last_synced: datetime | None = None


def can_import(user) -> bool:
    return user.has_perm(PERMISSION)


def _key_owner_name(visual: VisualDefinition) -> str:
    if visual.key_owner is None:
        return f"the {VisualDefinition.OFFLINE_UPLOADS}"
    return f"the Data Source {visual.key_owner.name}"


def _definition(item: Definition, key_owner: DataSource | None) -> VisualDefinition:
    kind = item.manifest.data["kind"]
    # The row lock makes an import wait for a transfer and then see the new owner.
    # No select_related: after the wait, PostgreSQL re-reads only the locked row.
    visual, _ = VisualDefinition.objects.select_for_update().get_or_create(
        key=item.manifest.data["key"], defaults={"kind": kind, "key_owner": key_owner}
    )
    if visual.kind != kind:
        message = f"The key {visual.key} is a {visual.kind} definition."
        raise ArtworkError(
            [Issue(item.manifest_path, item.manifest.line("kind"), message)]
        )
    if visual.key_owner_id != (key_owner.pk if key_owner else None):
        message = f"The key {visual.key} belongs to {_key_owner_name(visual)}."
        raise ArtworkError(
            [Issue(item.manifest_path, item.manifest.line("key"), message)]
        )
    return visual


def _import(user, provenance: Provenance, item: Definition) -> ImportedRevision:
    visual = _definition(item, provenance.data_source)
    revision, created = DefinitionRevision.objects.get_or_create(
        digest=item.digest,
        defaults={"definition": visual, "manifest": item.manifest.data},
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
        source_name=provenance.source_name,
        data_source=provenance.data_source,
        last_synced=provenance.last_synced,
        path=item.directory,
    )
    return ImportedRevision(revision, created)


def import_bundle(user, bundle_name: str, fileobj) -> list[ImportedRevision]:
    """Validate the whole bundle, then import every definition in one transaction."""
    if not can_import(user):
        raise PermissionDenied
    return _import_all(user, read_bundle(fileobj), Provenance(bundle_name))


def import_from_source(user, source: DataSource, prefix: str) -> list[ImportedRevision]:
    """Capture the files below the prefix, validate them, and import every definition."""
    if not can_import(user) or not (
        DataSource.objects.restrict(user, "view").filter(pk=source.pk).exists()
    ):
        raise PermissionDenied
    captured = read_source_files(user, source, prefix)
    provenance = Provenance(source.name, source, captured.last_synced)
    return _import_all(user, read_definitions(captured.entries), provenance)


def _import_all(
    user, definitions: list[Definition], provenance: Provenance
) -> list[ImportedRevision]:
    definitions = sorted(definitions, key=lambda item: item.manifest.data["key"])
    with transaction.atomic():
        # One lock order for all imports: every digest, sorted, before any write.
        for digest in sorted(
            {d for item in definitions for d in item.file_digests.values()}
        ):
            lock_digest(digest)
        results = [_import(user, provenance, item) for item in definitions]
        pks = {result.revision.pk for result in results}
        permitted = DefinitionRevision.objects.restrict(user, "add").filter(pk__in=pks)
        if permitted.count() != len(pks):
            raise PermissionDenied
    return results
