"""The plugin migrations load on the oldest NetBox release that the plugin supports."""

from django.apps import apps
from django.db.migrations.loader import MigrationLoader
from django.test import SimpleTestCase

PLUGIN = "netbox_device_view"
OLDEST_NETBOX = "4.5.0"
# The last migration of each NetBox app in NetBox 4.5.0.
OLDEST_MIGRATIONS = {
    "core": "0020_owner",
    "dcim": "0225_gfk_indexes",
    "extras": "0134_owner",
    "users": "0015_owner",
}


def _number(name: str) -> int:
    return int(name.split("_", 1)[0])


class MigrationDependencyTests(SimpleTestCase):
    def test_the_oldest_migrations_belong_to_the_minimum_netbox_version(self):
        self.assertEqual(apps.get_app_config(PLUGIN).min_version, OLDEST_NETBOX)

    def test_each_migration_depends_only_on_migrations_of_the_oldest_netbox(self):
        loader = MigrationLoader(None, ignore_no_migrations=True)
        for (app, name), migration in loader.disk_migrations.items():
            if app != PLUGIN:
                continue
            for dependency_app, dependency in migration.dependencies:
                # "__first__" comes from a swappable dependency, such as the user model.
                if dependency_app == PLUGIN or dependency == "__first__":
                    continue
                with self.subTest(migration=name, dependency=dependency):
                    self.assertIn(dependency_app, OLDEST_MIGRATIONS)
                    self.assertLessEqual(
                        _number(dependency), _number(OLDEST_MIGRATIONS[dependency_app])
                    )
