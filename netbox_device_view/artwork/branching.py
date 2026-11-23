"""Keep the artwork catalogue global when NetBox Branching is installed."""

from django.conf import settings

# Revision files and import records have no change log, so a branch merge cannot replay them.
CATALOGUE = frozenset(
    {
        "netbox_device_view.visualdefinition",
        "netbox_device_view.definitionrevision",
        "netbox_device_view.artworkblob",
        "netbox_device_view.revisionfile",
        "netbox_device_view.revisionimport",
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
