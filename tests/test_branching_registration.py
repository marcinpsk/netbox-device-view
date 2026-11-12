"""The plugin registers its catalogue resolver with an enabled NetBox Branching."""

import sys
import types
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase, override_settings

from netbox_device_view.artwork.branching import register_with_netbox_branching


def _netbox_branching_without_the_resolver_hook():
    """Return sys.modules entries of a NetBox Branching release that has no register_branching_resolver."""
    package = types.ModuleType("netbox_branching")
    package.utilities = types.ModuleType("netbox_branching.utilities")
    return {
        "netbox_branching": package,
        "netbox_branching.utilities": package.utilities,
    }


class RegistrationTests(SimpleTestCase):
    def test_an_enabled_netbox_branching_without_the_resolver_hook_stops_the_startup(
        self,
    ):
        plugins = [*settings.PLUGINS, "netbox_branching"]
        with (
            override_settings(PLUGINS=plugins),
            mock.patch.dict(sys.modules, _netbox_branching_without_the_resolver_hook()),
            self.assertRaises(ImportError),
        ):
            register_with_netbox_branching()

    def test_an_installed_netbox_branching_that_is_not_enabled_is_not_used(self):
        plugins = [name for name in settings.PLUGINS if name != "netbox_branching"]
        with (
            override_settings(PLUGINS=plugins),
            mock.patch.dict(sys.modules, _netbox_branching_without_the_resolver_hook()),
        ):
            register_with_netbox_branching()
