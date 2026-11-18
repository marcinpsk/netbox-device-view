"""Plugin behaviour in an active NetBox Branching branch; skipped when netbox-branching is not enabled."""

import re
import shutil
import tempfile
import unittest
import uuid
from pathlib import Path

from core.models import DataFile, DataSource, ObjectChange, ObjectType
from dcim.models import DeviceType, Manufacturer
from django.apps import apps
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import (
    RequestFactory,
    SimpleTestCase,
    TransactionTestCase,
    override_settings,
)
from django.urls import reverse
from netbox.context_managers import event_tracking
from users.constants import TOKEN_PREFIX
from users.models import Token, User

from netbox_device_view import datasource_import
from netbox_device_view.artwork.branching import CATALOGUE
from netbox_device_view.artwork.capture import CHANGED_FILE
from netbox_device_view.models import (
    MAX_REPR,
    Association,
    DefinitionRevision,
    DeviceView,
    VisualDefinition,
)

from .artwork_samples import DEVICE_FILES, FRONT_SVG, bundle, definition
from .permissions import grant
from .test_artwork_activation import ACTIVATE_URL
from .test_artwork_import import DEVICE_DIR
from .test_artwork_import import IMPORT_URL as ARTWORK_IMPORT_URL
from .test_artwork_key_transfer import CHANGELOG_URL as DEFINITION_CHANGELOG_URL
from .test_artwork_key_transfer import EDIT_URL as DEFINITION_EDIT_URL
from .test_artwork_source_import import SOURCE_IMPORT_URL
from .test_datasource_import import APPLY_URL, EXAMPLE_PATH, EXAMPLE_YAML
from .test_datasource_import_concurrency import ConcurrentApplyTestBase

BRANCHING = "netbox_branching" in settings.PLUGINS
REVISION_DELETE_URL = "plugins:netbox_device_view:definitionrevision_delete"
REVISION_API_URL = "plugins-api:netbox_device_view-api:definitionrevision-detail"


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
            {"netbox_device_view.deviceview", "netbox_device_view.association"},
        )


def long_device_type(save=False):
    """Return a device type whose manufacturer name and model have the maximum length."""
    manufacturer = Manufacturer(name="M" * 100, slug="m" * 100)
    device_type = DeviceType(manufacturer=manufacturer, model="X" * 100, slug="x" * 100)
    if save:
        manufacturer.save()
        device_type.manufacturer = manufacturer
        device_type.save()
    return device_type


class BranchObjectTextTests(SimpleTestCase):
    def test_the_text_of_each_branch_object_fits_its_change_record(self):
        # NetBox Branching copies str() of a branch object to ChangeDiff.object_repr without a cut.
        device_type = long_device_type()
        revision = DefinitionRevision(
            definition=VisualDefinition(key="acme/" + "a" * 123), digest="0" * 64
        )
        for obj in (
            DeviceView(device_type=device_type),
            Association(device_type=device_type, revision=revision),
        ):
            with self.subTest(type(obj).__name__):
                self.assertLessEqual(len(str(obj)), MAX_REPR)
                self.assertTrue(
                    str(obj).startswith(device_type.full_name[: MAX_REPR - 1])
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
            follow=True,
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
        from netbox_branching.choices import BranchStatusChoices
        from netbox_branching.utilities import activate_branch

        self.upload()
        revision = DefinitionRevision.objects.get()
        # Branch.merge() replays nothing without a staged change, so stage one.
        request = RequestFactory().get(reverse("home"))
        request.id, request.user = uuid.uuid4(), self.user
        with activate_branch(self.branch), event_tracking(request):
            Manufacturer.objects.create(name="Branch Only", slug="branch-only")

        self.branch.merge(user=self.user)

        self.branch.refresh_from_db()
        self.assertEqual(self.branch.status, BranchStatusChoices.MERGED)
        self.assertTrue(Manufacturer.objects.filter(slug="branch-only").exists())
        self.assertEqual(DefinitionRevision.objects.get(), revision)
        self.assertEqual(
            sorted(revision.files.values_list("path", flat=True)),
            ["front.svg", "manifest.yaml", "rear.svg"],
        )
        self.assertEqual(revision.imports.get().source_name, "sw24.zip")

    def test_the_revision_change_log_reads_main_in_a_branch(self):
        del self.client.cookies["active_branch"]
        self.upload()
        self.client.cookies["active_branch"] = self.branch.schema_id
        grant(self.user, ObjectChange, "view")
        revision = DefinitionRevision.objects.get()
        change = ObjectChange.objects.using("default").get(
            changed_object_type=ObjectType.objects.get_for_model(DefinitionRevision),
            changed_object_id=revision.pk,
        )

        response = self.client.get(
            reverse(
                "plugins:netbox_device_view:definitionrevision_changelog",
                args=[revision.pk],
            )
        )

        self.assertContains(response, change.get_absolute_url())
        # The selector button shows the active branch; its dropdown lists every branch.
        selected = re.search(
            r'mdi-source-branch"></i>\s*(.*?)\s*</button>', response.content.decode()
        )
        self.assertEqual(selected[1], self.branch.name)


class ArtworkSourceImportInBranchTests(ArtworkBranchTestCase):
    def setUp(self):
        super().setUp()
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        for path, content in definition(DEVICE_DIR, DEVICE_FILES).items():
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            (root / path).write_text(content)
        self.source = DataSource.objects.create(
            name="Artwork", type="local", source_url=f"file://{root}", enabled=True
        )
        self.source.sync()
        grant(self.user, DataSource, "view")
        grant(self.user, DataFile, "view")

    def import_source(self):
        return self.client.post(
            reverse(SOURCE_IMPORT_URL),
            {"data_source": self.source.pk, "prefix": ""},
            follow=True,
        )

    def test_a_data_source_import_in_a_branch_stores_the_revision_in_main(self):
        response = self.import_source()

        self.assertContains(response, "Imported")
        revision = DefinitionRevision.objects.get()
        self.assertEqual(revision.definition.key_owner, self.source)
        self.assertEqual(revision.imports.get().data_source, self.source)

    def test_a_torn_file_found_in_a_branch_is_read_again_by_the_next_sync(self):
        front = DataFile.objects.filter(path=f"{DEVICE_DIR}/front.svg")
        front.update(data=FRONT_SVG.replace("#333", "#222").encode())

        self.assertContains(self.import_source(), CHANGED_FILE, status_code=400)
        self.assertEqual(front.get().hash, "")

        self.source.sync()

        self.assertContains(self.import_source(), "Imported")


class LongNameActivationInBranchTests(ArtworkBranchTestCase):
    def setUp(self):
        # The branch copies main when it is provisioned.
        self.device_type = long_device_type(save=True)
        super().setUp()

    def test_an_activation_for_a_type_with_long_names_has_its_change_record(self):
        from netbox_branching.models import ChangeDiff

        self.upload()
        revision = DefinitionRevision.objects.get()
        grant(self.user, Association, "view", "add")
        grant(self.user, DeviceType, "view")

        response = self.client.post(
            reverse(ACTIVATE_URL, args=[revision.pk]),
            {"device_type": self.device_type.pk},
        )

        self.assertEqual(response.status_code, 302)
        diff = ChangeDiff.objects.using("default").get(
            object_type=ObjectType.objects.get_for_model(Association)
        )
        self.assertTrue(
            diff.object_repr.startswith(self.device_type.full_name[: MAX_REPR - 1])
        )


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

    def test_the_definition_change_log_reads_main_in_a_branch(self):
        del self.client.cookies["active_branch"]
        self.upload()
        self.client.cookies["active_branch"] = self.branch.schema_id
        visual = VisualDefinition.objects.get()
        grant(self.user, VisualDefinition, "view")
        grant(self.user, ObjectChange, "view")
        change = ObjectChange.objects.using("default").get(
            changed_object_type=ObjectType.objects.get_for_model(VisualDefinition),
            changed_object_id=visual.pk,
        )

        response = self.client.get(reverse(DEFINITION_CHANGELOG_URL, args=[visual.pk]))

        self.assertContains(response, change.get_absolute_url())
        self.assertContains(response, self.branch.name)


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

    def test_a_revision_active_only_in_a_branch_cannot_be_deleted_in_main(self):
        self.upload()
        revision = DefinitionRevision.objects.get()
        grant(self.user, Association, "view", "add")
        grant(self.user, DeviceType, "view")
        grant(self.user, DefinitionRevision, "delete")
        activation = self.client.post(
            reverse(ACTIVATE_URL, args=[revision.pk]),
            {"device_type": self.device_type.pk},
        )
        self.assertEqual(activation.status_code, 302)
        del self.client.cookies["active_branch"]

        page = self.client.post(
            reverse(REVISION_DELETE_URL, args=[revision.pk]),
            {"confirm": True},
            follow=True,
        )
        token = Token.objects.create(user=self.user)
        api = self.client.delete(
            reverse(REVISION_API_URL, args=[revision.pk]),
            HTTP_AUTHORIZATION=f"Bearer {TOKEN_PREFIX}{token.key}.{token.token}",
        )

        self.assertContains(page, "Unable to delete")
        self.assertContains(page, self.branch.get_absolute_url())
        self.assertEqual(api.status_code, 409)
        self.assertIn(f"{self.branch} ({self.branch.pk})", api.json()["detail"])
        self.assertTrue(DefinitionRevision.objects.filter(pk=revision.pk).exists())

    def test_a_revision_that_no_branch_uses_can_be_deleted(self):
        self.upload()
        revision = DefinitionRevision.objects.get()
        grant(self.user, DefinitionRevision, "delete")
        del self.client.cookies["active_branch"]

        response = self.client.post(
            reverse(REVISION_DELETE_URL, args=[revision.pk]), {"confirm": True}
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(DefinitionRevision.objects.filter(pk=revision.pk).exists())

    def test_a_rejected_activation_in_a_branch_leaves_no_change_record(self):
        from netbox_branching.models import ChangeDiff
        from netbox_branching.utilities import activate_branch

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
        with activate_branch(self.branch):
            self.assertFalse(
                Association.objects.filter(device_type=self.device_type).exists()
            )
        self.assertFalse(
            ChangeDiff.objects.using("default")
            .filter(object_type=ObjectType.objects.get_for_model(Association))
            .exists()
        )
