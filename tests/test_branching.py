"""Plugin behaviour in an active NetBox Branching branch; skipped when netbox-branching is not enabled."""

import shutil
import tempfile
import unittest
from pathlib import Path

from core.models import DataFile, DataSource, ObjectChange, ObjectType
from dcim.models import DeviceType, Manufacturer
from django.apps import apps
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TransactionTestCase, override_settings
from django.urls import reverse
from users.models import User

from netbox_device_view import datasource_import
from netbox_device_view.artwork.branching import CATALOGUE
from netbox_device_view.models import (
    Association,
    BindingOverride,
    DefinitionRevision,
    DeviceView,
    VisualDefinition,
)

from .artwork_samples import DEVICE_FILES, bundle, definition
from .permissions import grant
from .test_artwork_activation import ACTIVATE_URL
from .test_artwork_import import DEVICE_DIR
from .test_artwork_import import IMPORT_URL as ARTWORK_IMPORT_URL
from .test_artwork_key_transfer import EDIT_URL as DEFINITION_EDIT_URL
from .test_artwork_source_import import SOURCE_IMPORT_URL
from .test_binding_override_pages import ADD_URL as OVERRIDE_ADD_URL
from .test_binding_override_pages import rows
from .test_datasource_import import APPLY_URL, EXAMPLE_PATH, EXAMPLE_YAML
from .test_datasource_import_concurrency import ConcurrentApplyTestBase

BRANCHING = "netbox_branching" in settings.PLUGINS


def provision_branch(name):
    from netbox_branching.models import Branch

    branch = Branch(name=name)
    branch.save(provision=False)
    branch.provision(user=None)
    branch.refresh_from_db()
    return branch


@unittest.skipUnless(BRANCHING, "netbox-branching is not enabled")
class DataSourceImportInBranchTests(ConcurrentApplyTestBase):
    def setUp(self):
        super().setUp()
        self.branch = provision_branch("Layouts")
        self.addCleanup(self.branch.deprovision)
        self.clients[0].cookies["active_branch"] = self.branch.schema_id

    def test_import_creates_the_device_view_in_the_branch_only(self):
        from netbox_branching.utilities import activate_branch

        response = self.clients[0].post(
            reverse(APPLY_URL), {"token": self.token(EXAMPLE_PATH)}
        )

        self.assertContains(response, "Created: 1")
        self.assertFalse(DeviceView.objects.exists())
        with activate_branch(self.branch):
            self.assertEqual(
                DeviceView.objects.get().yaml_layout, EXAMPLE_YAML.read_text()
            )

    def test_a_review_in_a_branch_does_not_apply_in_main(self):
        from netbox_branching.utilities import activate_branch

        token = self.token(EXAMPLE_PATH)
        del self.clients[0].cookies["active_branch"]

        response = self.clients[0].post(reverse(APPLY_URL), {"token": token})

        self.assertContains(response, datasource_import.OTHER_BRANCH, status_code=400)
        self.assertFalse(DeviceView.objects.exists())
        with activate_branch(self.branch):
            self.assertFalse(DeviceView.objects.exists())

    def test_a_rejected_import_leaves_no_change_records(self):
        from netbox_branching.models import ChangeDiff
        from netbox_branching.utilities import activate_branch

        with activate_branch(self.branch):
            other_type = DeviceType.objects.create(
                manufacturer=self.device_type.manufacturer, model="Other", slug="other"
            )
        token = self.token_for(
            [
                (EXAMPLE_PATH, self.device_type),
                ("examples/yaml/Cisco/Other.yaml", other_type),
            ]
        )
        # The second target gets a device view after the review, so its row is stale.
        with activate_branch(self.branch):
            DeviceView.objects.create(device_type=other_type)

        response = self.clients[0].post(reverse(APPLY_URL), {"token": token})

        self.assertEqual(response.status_code, 400)
        with activate_branch(self.branch):
            self.assertEqual(DeviceView.objects.count(), 1)
        self.assertFalse(ChangeDiff.objects.exists())
        self.assertFalse(ObjectChange.objects.using("default").exists())


class CatalogueModelTests(SimpleTestCase):
    def test_every_plugin_model_that_refers_to_no_type_is_in_the_catalogue(self):
        labels = {
            model._meta.label_lower
            for model in apps.get_app_config("netbox_device_view").get_models()
        }

        # A branch can change device types and module types, so their links are branched too.
        self.assertEqual(
            labels - CATALOGUE,
            {
                "netbox_device_view.deviceview",
                "netbox_device_view.association",
                "netbox_device_view.bindingoverride",
            },
        )


@unittest.skipUnless(BRANCHING, "netbox-branching is not enabled")
class ArtworkBranchTestCase(TransactionTestCase):
    """An artwork importer whose test client works in a new branch."""

    def setUp(self):
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media)
        media_override = override_settings(MEDIA_ROOT=media)
        media_override.enable()
        self.addCleanup(media_override.disable)
        self.user = User.objects.create_user(username="importer")
        self.client.force_login(self.user)
        grant(self.user, DefinitionRevision, "view", "add")
        self.branch = provision_branch("Artwork")
        self.addCleanup(self.branch.deprovision)
        self.client.cookies["active_branch"] = self.branch.schema_id

    def upload(self):
        return self.client.post(
            reverse(ARTWORK_IMPORT_URL),
            {"bundle": SimpleUploadedFile("sw24.zip", bundle())},
        )


class ArtworkImportInBranchTests(ArtworkBranchTestCase):
    def test_a_bundle_import_in_a_branch_stores_the_revision_in_main(self):
        response = self.upload()

        self.assertContains(response, "Imported")
        revision = DefinitionRevision.objects.get()
        self.assertEqual(revision.definition.key, "acme/sw-24")
        self.assertEqual(
            sorted(revision.files.values_list("path", flat=True)),
            ["front.svg", "manifest.yaml", "rear.svg"],
        )
        self.assertEqual(revision.imports.get().source_name, "sw24.zip")

    def test_a_branch_merge_leaves_the_imported_revision_in_main(self):
        self.upload()

        self.branch.merge(user=self.user)

        self.assertEqual(DefinitionRevision.objects.get().definition.key, "acme/sw-24")


class ArtworkSourceImportInBranchTests(ArtworkBranchTestCase):
    def test_a_data_source_import_in_a_branch_stores_the_revision_in_main(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        for path, content in definition(DEVICE_DIR, DEVICE_FILES).items():
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            (root / path).write_text(content)
        source = DataSource.objects.create(
            name="Artwork", type="local", source_url=f"file://{root}", enabled=True
        )
        source.sync()
        grant(self.user, DataSource, "view")
        grant(self.user, DataFile, "view")

        response = self.client.post(
            reverse(SOURCE_IMPORT_URL), {"data_source": source.pk, "prefix": ""}
        )

        self.assertContains(response, "Imported")
        revision = DefinitionRevision.objects.get()
        self.assertEqual(revision.definition.key_owner, source)
        self.assertEqual(revision.imports.get().data_source, source)


class KeyTransferInBranchTests(ArtworkBranchTestCase):
    def test_a_key_transfer_in_a_branch_changes_main_at_once(self):
        self.upload()
        visual = VisualDefinition.objects.get()
        source = DataSource.objects.create(
            name="Artwork", type="local", source_url="file:///artwork", enabled=True
        )
        grant(self.user, VisualDefinition, "view", "change")
        grant(self.user, DataSource, "view")

        response = self.client.post(
            reverse(DEFINITION_EDIT_URL, args=[visual.pk]), {"key_owner": source.pk}
        )

        self.assertEqual(response.status_code, 302)
        visual.refresh_from_db()
        self.assertEqual(visual.key_owner, source)
        self.assertEqual(
            ObjectChange.objects.using("default")
            .filter(changed_object_id=visual.pk, action="update")
            .count(),
            1,
        )


class ActivationInBranchTests(ArtworkBranchTestCase):
    def setUp(self):
        # The branch copies main when it is provisioned.
        self.device_type = DeviceType.objects.create(
            manufacturer=Manufacturer.objects.create(name="ACME", slug="acme"),
            model="SW-24",
            slug="sw-24",
        )
        super().setUp()

    def test_an_activation_in_a_branch_changes_main_at_the_merge(self):
        from netbox_branching.utilities import activate_branch

        self.upload()
        revision = DefinitionRevision.objects.get()
        grant(self.user, Association, "view", "add")
        grant(self.user, DeviceType, "view")

        response = self.client.post(
            reverse(ACTIVATE_URL, args=[revision.pk]),
            {"device_type": self.device_type.pk},
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(Association.objects.exists())
        with activate_branch(self.branch):
            self.assertEqual(Association.objects.get().revision_id, revision.pk)

        self.branch.merge(user=self.user)

        self.assertEqual(Association.objects.get().device_type, self.device_type)

    def test_a_rejected_activation_in_a_branch_leaves_no_change_record(self):
        from netbox_branching.models import ChangeDiff

        self.upload()
        revision = DefinitionRevision.objects.get()
        grant(
            self.user,
            Association,
            "view",
            "add",
            constraints={"device_type__model": "Other"},
        )
        grant(self.user, DeviceType, "view")

        response = self.client.post(
            reverse(ACTIVATE_URL, args=[revision.pk]),
            {"device_type": self.device_type.pk},
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(
            ChangeDiff.objects.using("default")
            .filter(object_type=ObjectType.objects.get_for_model(Association))
            .exists()
        )


class BindingOverrideInBranchTests(ArtworkBranchTestCase):
    def setUp(self):
        from netbox_branching.utilities import activate_branch

        # The branch copies main when it is provisioned.
        self.device_type = DeviceType.objects.create(
            manufacturer=Manufacturer.objects.create(name="ACME", slug="acme"),
            model="SW-24",
            slug="sw-24",
        )
        super().setUp()
        self.upload()
        with activate_branch(self.branch):
            Association.objects.create(
                device_type=self.device_type, revision=DefinitionRevision.objects.get()
            )
        grant(self.user, DeviceType, "view")

    def add_override(self):
        return self.client.post(
            reverse(OVERRIDE_ADD_URL),
            {
                "device_type": self.device_type.pk,
                "connector_key": "con-eth1",
                **rows(("interface", "eth2")),
            },
        )

    def test_an_override_in_a_branch_changes_main_at_the_merge(self):
        from netbox_branching.utilities import activate_branch

        grant(self.user, BindingOverride, "view", "add")

        response = self.add_override()

        self.assertEqual(response.status_code, 302)
        self.assertFalse(BindingOverride.objects.exists())
        with activate_branch(self.branch):
            self.assertEqual(BindingOverride.objects.get().connector_key, "con-eth1")

        self.branch.merge(user=self.user)

        self.assertEqual(BindingOverride.objects.get().device_type, self.device_type)

    def test_a_rejected_override_in_a_branch_leaves_no_change_record(self):
        from netbox_branching.models import ChangeDiff

        grant(
            self.user,
            BindingOverride,
            "view",
            "add",
            constraints={"connector_key": "con-psu"},
        )

        response = self.add_override()

        self.assertContains(response, "permissions violation")
        self.assertFalse(
            ChangeDiff.objects.using("default")
            .filter(object_type=ObjectType.objects.get_for_model(BindingOverride))
            .exists()
        )
