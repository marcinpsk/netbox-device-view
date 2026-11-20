"""Keep the artwork catalogue global when NetBox Branching is installed."""

from contextlib import nullcontext

from django.conf import settings
from django.db import connections
from django.db.models import ProtectedError
from django.db.models.signals import pre_delete
from django.utils.translation import gettext as _
from psycopg import sql

# Revision files and import records have no change log, so a branch merge cannot replay them.
CATALOGUE = frozenset(
    {
        "netbox_device_view.visualdefinition",
        "netbox_device_view.definitionrevision",
        "netbox_device_view.artworkblob",
        "netbox_device_view.revisionfile",
        "netbox_device_view.revisionimport",
        "netbox_device_view.sourcesyncrun",
    }
)


def keep_catalogue_in_main(model):
    """Return False for an artwork catalogue model, so NetBox Branching routes it to main."""
    return False if model._meta.label_lower in CATALOGUE else None


def register_with_netbox_branching():
    if "netbox_branching" not in settings.PLUGINS:
        return
    # A release without the resolver hook would put the catalogue into branches, so fail at startup.
    from netbox_branching.utilities import register_branching_resolver

    register_branching_resolver(keep_catalogue_in_main)
    pre_delete.connect(
        protect_branch_revisions,
        sender="netbox_device_view.DefinitionRevision",
        dispatch_uid="netbox_device_view.protect_branch_revisions",
    )


def protect_branch_revisions(sender, instance, using, **kwargs):
    """Refuse to delete a revision that an association or a pin in a branch schema uses."""
    # A branch schema has no foreign keys, so PROTECT on the revision fields does not see it.
    from netbox_branching.models import Branch

    from ..models import Association, InstancePin

    branches = {branch.schema_name: branch for branch in Branch.objects.using(using)}
    if not branches:
        return
    users = set()
    with connections[using].cursor() as cursor:
        for model in (Association, InstancePin):
            table = model._meta.db_table
            column = model._meta.get_field("revision").column
            # A schema without the table is not provisioned or not migrated, so it has no rows.
            cursor.execute(
                "SELECT table_schema FROM information_schema.tables"
                " WHERE table_name = %s AND table_schema = ANY(%s)"
                " ORDER BY table_schema",
                [table, list(branches)],
            )
            for (schema,) in cursor.fetchall():
                cursor.execute(
                    sql.SQL("SELECT EXISTS (SELECT 1 FROM {} WHERE {} = %s)").format(
                        sql.Identifier(schema, table), sql.Identifier(column)
                    ),
                    [instance.pk],
                )
                if cursor.fetchone()[0]:
                    users.add(branches[schema])
    if users:
        raise ProtectedError(
            _("The revision is the active or pinned artwork in a branch."), users
        )


def main_schema():
    """Return a context manager that reads from main while a branch is active."""
    if "netbox_branching" not in settings.PLUGINS:
        return nullcontext()
    from netbox_branching.utilities import deactivate_branch

    return deactivate_branch()
